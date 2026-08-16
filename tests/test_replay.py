"""The replay index, and the tree pywb discovers it through.

The tests that matter here are the ones for failures that stay invisible: an
index whose filenames collide across captures looks perfect until the second
capture, and a binary search that is off by one line silently loses versions
rather than crashing.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from cairn.config import Settings
from cairn.services import replay, storage

# Symlinks need Developer Mode or elevation on Windows. Replay runs in the
# container; these cover the tree-building half.
needs_symlinks = pytest.mark.skipif(
    os.name == "nt", reason="symlinks need elevation on Windows; replay runs on Linux"
)

ARCHIVE_PATH = "Unfiled/example-blog"
URL = "https://example.blogspot.com/"
POST = "https://example.blogspot.com/2026/08/post.html"


def write_warc(path: Path, records: list[tuple[str, str, bytes]]) -> None:
    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        writer = WARCWriter(fh, gzip=True)
        for url, when, body in records:
            headers = StatusAndHeaders(
                "200 OK", [("Content-Type", "text/html")], protocol="HTTP/1.1"
            )
            writer.write_record(
                writer.create_warc_record(
                    url,
                    "response",
                    payload=io.BytesIO(body),
                    http_headers=headers,
                    warc_headers_dict={"WARC-Date": when},
                )
            )


def make_capture(settings: Settings, dir_name: str, records: list[tuple[str, str, bytes]]) -> Path:
    out = storage.ensure_capture_dirs(settings, ARCHIVE_PATH, dir_name)
    warc = out / storage.WARC_DIR / "part-00000.warc.gz"
    write_warc(warc, records)
    return warc


@pytest.fixture
def site_tree(settings: Settings) -> Settings:
    storage.ensure_site_dirs(settings, ARCHIVE_PATH)
    return settings


# ── the filename trap ────────────────────────────────────────────────────


def test_index_filenames_are_site_relative(site_tree: Settings) -> None:
    """Not the basename. Every capture writes `warc/part-00000.warc.gz`, so a
    basename is the same string for all of them — pywb then answers 503 rather
    than choosing, and switching captures stops working. Site-relative is also
    what survives moving the site to another folder."""
    make_capture(site_tree, "20260810T120000Z-full-wget", [(URL, "2026-08-10T12:00:00Z", b"one")])

    result = replay.build_index(site_tree, ARCHIVE_PATH)

    assert result.records == 1
    line = result.path.read_text(encoding="utf-8").splitlines()[0]
    filename = json.loads(line.split(" ", 2)[2])["filename"]
    assert filename == "captures/20260810T120000Z-full-wget/warc/part-00000.warc.gz"


def test_two_captures_of_one_url_stay_distinguishable(site_tree: Settings) -> None:
    """M3's exit criterion, at the level the index decides it."""
    make_capture(site_tree, "20260810T120000Z-full-wget", [(URL, "2026-08-10T12:00:00Z", b"first")])
    make_capture(
        site_tree, "20260811T120000Z-full-wget", [(URL, "2026-08-11T12:00:00Z", b"second")]
    )

    replay.build_index(site_tree, ARCHIVE_PATH)
    versions = replay.lookup(site_tree, ARCHIVE_PATH, URL)

    assert [v.timestamp for v in versions] == ["20260810120000", "20260811120000"]
    assert len({v.filename for v in versions}) == 2, "captures must not share a filename"


def test_a_basename_index_is_refused_rather_than_written(site_tree: Settings) -> None:
    """The guard for the failure above, in case the indexer's behaviour changes."""
    with pytest.raises(replay.ReplayError, match="site-relative"):
        replay._assert_relative(
            ['com,example)/ 20260810120000 {"filename": "part-00000.warc.gz"}\n'],
            ["captures/20260810T120000Z-full-wget/warc/part-00000.warc.gz"],
        )


# ── reading it back ──────────────────────────────────────────────────────


def test_index_is_sorted_and_rebuilds_identically(site_tree: Settings) -> None:
    """Byte-identical on a rebuild, or "did the index change?" is unanswerable."""
    make_capture(
        site_tree,
        "20260810T120000Z-full-wget",
        [
            (POST, "2026-08-10T12:00:00Z", b"post"),
            (URL, "2026-08-10T12:00:01Z", b"home"),
        ],
    )
    first = replay.build_index(site_tree, ARCHIVE_PATH).path.read_bytes()
    second = replay.build_index(site_tree, ARCHIVE_PATH).path.read_bytes()

    assert first == second
    lines = first.decode().splitlines()
    assert lines == sorted(lines)


def test_lookup_agrees_with_a_linear_scan(site_tree: Settings, tmp_path: Path) -> None:
    """The binary search, against the obvious implementation.

    An off-by-one here does not crash — it returns the wrong slice, so a page
    quietly shows fewer versions than the archive holds.
    """
    index = replay.index_path(site_tree, ARCHIVE_PATH)
    index.parent.mkdir(parents=True, exist_ok=True)

    keys = [f"com,example)/page-{i:04d}" for i in range(400)]
    lines = []
    for key in keys:
        for ts in ("20260810120000", "20260811120000"):
            lines.append(f'{key} {ts} {{"url": "https://example.com/{key}", "filename": "w.gz"}}\n')
    lines.sort()
    # Bytes, not write_text: text mode would translate the newlines on Windows
    # and the comparison below would be measuring that instead of the search.
    index.write_bytes("".join(lines).encode("utf-8"))

    for probe in (keys[0], keys[7], keys[199], keys[-1]):
        prefix = f"{probe} ".encode()
        with open(index, "rb") as fh:
            offset = replay._first_line_at_or_after(fh, prefix)
            fh.seek(offset)
            found = [ln for ln in fh if ln.startswith(prefix)]
        expected = [ln.encode() for ln in lines if ln.startswith(prefix.decode())]
        assert found == expected, f"binary search disagreed at {probe}"

    # A key before everything and after everything: the two ends bisection
    # gets wrong most often.
    with open(index, "rb") as fh:
        assert replay._first_line_at_or_after(fh, b"aaa ") == 0
        assert replay._first_line_at_or_after(fh, b"zzz ") == index.stat().st_size


def test_lookup_canonicalises_the_url_like_the_index_does(site_tree: Settings) -> None:
    """The index key is a SURT. A caller passing the URL with a default port,
    a capitalised host, or no trailing slash must still find it."""
    make_capture(site_tree, "20260810T120000Z-full-wget", [(URL, "2026-08-10T12:00:00Z", b"home")])
    replay.build_index(site_tree, ARCHIVE_PATH)

    for variant in (URL, "https://Example.Blogspot.com/", "https://example.blogspot.com:443/"):
        assert replay.lookup(site_tree, ARCHIVE_PATH, variant), variant


def test_no_captures_yields_an_empty_index_not_an_error(site_tree: Settings) -> None:
    result = replay.build_index(site_tree, ARCHIVE_PATH)
    assert result.records == 0
    assert result.path.is_file()
    assert replay.lookup(site_tree, ARCHIVE_PATH, URL) == []


def test_reading_a_record_returns_the_archived_body(site_tree: Settings) -> None:
    make_capture(
        site_tree, "20260810T120000Z-full-wget", [(URL, "2026-08-10T12:00:00Z", b"<h1>hello</h1>")]
    )
    replay.build_index(site_tree, ARCHIVE_PATH)

    found = replay.lookup(site_tree, ARCHIVE_PATH, URL)
    parsed = replay.read_record(site_tree, ARCHIVE_PATH, found[0])

    assert parsed.rec_headers.get_header("WARC-Target-URI") == URL
    assert parsed.content_stream().read() == b"<h1>hello</h1>"


def test_a_record_pointing_outside_the_site_is_refused(site_tree: Settings) -> None:
    """`filename` reaches the filesystem, so it goes through containment even
    though it comes from an index we generated."""
    escaping = replay.CdxRecord(
        urlkey="com,example)/",
        timestamp="20260810120000",
        url=URL,
        mime="text/html",
        status="200",
        digest=None,
        filename="../../../etc/passwd",
        offset=0,
        length=1,
    )
    with pytest.raises((storage.StoragePathError, replay.ReplayError)):
        replay.read_record(site_tree, ARCHIVE_PATH, escaping)


# ── the collection tree ──────────────────────────────────────────────────


def test_collections_are_keyed_by_id_not_slug() -> None:
    """Renaming or moving a site must not change its replay URL."""
    assert replay.collection_name(42) == "site-42"


@needs_symlinks
def test_linking_points_pywb_at_the_site(site_tree: Settings) -> None:
    coll = replay.link_collection(site_tree, 42, ARCHIVE_PATH)

    site_root = storage.site_dir(site_tree, ARCHIVE_PATH)
    assert (coll / replay.ARCHIVE_LINK).resolve() == site_root.resolve()
    assert (coll / replay.INDEXES_LINK).resolve() == (site_root / storage.INDEX_DIR).resolve()


@needs_symlinks
def test_relinking_an_existing_collection_is_idempotent(site_tree: Settings) -> None:
    first = replay.link_collection(site_tree, 42, ARCHIVE_PATH)
    second = replay.link_collection(site_tree, 42, ARCHIVE_PATH)
    assert first == second
    assert (second / replay.ARCHIVE_LINK).is_symlink()


@needs_symlinks
def test_moving_a_site_re_points_its_collection(
    db: object, settings: Settings, tmp_path: Path
) -> None:
    """The M4 half of the M3 design.

    A collection is two symlinks into the site directory, so a folder move
    leaves them dangling. Probed against pywb 2.9.1: a stale link answers 404
    and a re-pointed one answers correctly on the very next request, with no
    restart — so re-pointing them is the whole of what a move owes replay.
    """
    from sqlalchemy.orm import Session

    from cairn.services import folders, moves
    from cairn.services import sites as site_service

    session = db  # the fixture is a live Session against the app's database
    assert isinstance(session, Session)

    site = site_service.create_site(session, settings, seed_url="https://example.com/")
    replay.link_collection(settings, site.id, site.archive_path)
    target = folders.create_folder(session, settings, name="Archive", parent_id=None)

    moves.move_site(session, settings, site, target)

    coll = replay.collection_dir(settings, site.id)
    assert (coll / replay.ARCHIVE_LINK).resolve() == storage.site_dir(
        settings, site.archive_path
    ).resolve()
    assert (coll / replay.ARCHIVE_LINK).exists(), "the collection is dangling after the move"


def test_generated_config_discovers_collections_rather_than_listing_them(
    settings: Settings,
) -> None:
    """Listing them would mean restarting pywb every time a site is added."""
    import yaml

    path = replay.write_config(settings)
    config = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert config["collections_root"] == "collections"
    assert "collections" not in config or not isinstance(config.get("collections"), dict)
    assert config["framed_replay"] is True
    assert config["enable_content_security_policy"] is True
    assert config["port"] == settings.replay_port
    assert "do not hand-edit" in path.read_text(encoding="utf-8")


# ── records that are not pages ───────────────────────────────────────────
#
# From a real run: after capturing a gated blog, replay showed pywb's "could
# not be found in this collection" for a URL the person had never entered. The
# archive held one redirect and three of wget's own bookkeeping records, so
# four records looked like four pages and the iframe loaded anyway.


def write_mixed_warc(path: Path) -> None:
    """One redirect, one page, and the three records wget writes about itself."""
    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        writer = WARCWriter(fh, gzip=True)

        redirect = StatusAndHeaders(
            "302 Moved Temporarily",
            [("Location", "https://www.blogger.com/interstitial/blog?u=" + URL)],
            protocol="HTTP/1.1",
        )
        writer.write_record(writer.create_warc_record(URL, "response", http_headers=redirect))

        ok = StatusAndHeaders("200 OK", [("Content-Type", "text/html")], protocol="HTTP/1.1")
        writer.write_record(
            writer.create_warc_record(
                POST, "response", payload=io.BytesIO(b"<html>real</html>"), http_headers=ok
            )
        )

        for name in ("MANIFEST.txt", "wget.log", "wget_arguments.txt"):
            writer.write_record(
                writer.create_warc_record(
                    f"metadata://gnu.org/software/wget/warc/{name}",
                    "metadata",
                    payload=io.BytesIO(b"wget talking about itself"),
                    warc_content_type="text/plain",
                )
            )


def test_the_crawlers_own_records_are_left_out_of_the_index(site_tree: Settings) -> None:
    out = storage.ensure_capture_dirs(site_tree, ARCHIVE_PATH, "20260810T120000Z-full-wget")
    write_mixed_warc(out / storage.WARC_DIR / "part-00000.warc.gz")

    result = replay.build_index(site_tree, ARCHIVE_PATH)
    urls = [record.url for record in replay.index_records(site_tree, ARCHIVE_PATH)]

    assert result.records == 2, urls
    assert not any(u.startswith("metadata:") for u in urls)
    assert sorted(urls) == sorted([URL, POST])


def test_a_redirect_only_archive_has_no_replayable_pages(site_tree: Settings) -> None:
    """What a gated blog leaves behind. The iframe must not be offered for it."""
    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    out = storage.ensure_capture_dirs(site_tree, ARCHIVE_PATH, "20260810T120000Z-full-wget")
    warc = out / storage.WARC_DIR / "part-00000.warc.gz"
    warc.parent.mkdir(parents=True, exist_ok=True)
    with open(warc, "wb") as fh:
        writer = WARCWriter(fh, gzip=True)
        headers = StatusAndHeaders(
            "302 Moved Temporarily",
            [("Location", "https://www.blogger.com/interstitial/blog?u=" + URL)],
            protocol="HTTP/1.1",
        )
        writer.write_record(writer.create_warc_record(URL, "response", http_headers=headers))

    replay.build_index(site_tree, ARCHIVE_PATH)
    records, _mtime = replay.index_stats(site_tree, ARCHIVE_PATH)

    assert records == 1
    assert replay.replayable_pages(site_tree, ARCHIVE_PATH) == 0


def test_a_page_counts_as_replayable(site_tree: Settings) -> None:
    make_capture(site_tree, "20260810T120000Z-full-wget", [(URL, "2026-08-10T12:00:00Z", b"hi")])
    replay.build_index(site_tree, ARCHIVE_PATH)
    assert replay.replayable_pages(site_tree, ARCHIVE_PATH) == 1


def test_the_existence_check_stops_at_the_first_page(site_tree: Settings, monkeypatch) -> None:
    """Not "is it faster" but "does it stop", which is the only version of this
    that stays true as archives grow.

    Reported as the replay tab freezing on a large blog while a small one was
    fine. The status endpoint parsed every line of the index to produce a count
    whose only use was a comparison against zero — 1,435 ms on a 500,000-record
    archive, measured, and paid on every open of the tab.
    """
    make_capture(site_tree, "20260810T120000Z-full-wget", [(URL, "2026-08-10T12:00:00Z", b"hi")])
    replay.build_index(site_tree, ARCHIVE_PATH)

    real = replay.index_records
    seen = 0

    def counting(*args: object, **kwargs: object):
        nonlocal seen
        for record in real(*args, **kwargs):  # type: ignore[arg-type]
            seen += 1
            yield record

    monkeypatch.setattr(replay, "index_records", counting)

    assert replay.has_replayable_page(site_tree, ARCHIVE_PATH) is True
    assert seen == 1, f"read {seen} records to answer a yes/no question"


def test_the_existence_check_still_says_no_for_a_redirect_only_archive(
    site_tree: Settings,
) -> None:
    """The early exit must not turn "nothing is replayable" into a false yes —
    that would put an iframe in front of a gated blog, which is the failure the
    check was written for."""
    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    out = storage.ensure_capture_dirs(site_tree, ARCHIVE_PATH, "20260810T120000Z-full-wget")
    warc = out / storage.WARC_DIR / "part-00000.warc.gz"
    warc.parent.mkdir(parents=True, exist_ok=True)
    with open(warc, "wb") as fh:
        writer = WARCWriter(fh, gzip=True)
        headers = StatusAndHeaders(
            "302 Moved Temporarily",
            [("Location", "https://www.blogger.com/interstitial/blog?u=" + URL)],
            protocol="HTTP/1.1",
        )
        writer.write_record(writer.create_warc_record(URL, "response", http_headers=headers))

    replay.build_index(site_tree, ARCHIVE_PATH)
    assert replay.has_replayable_page(site_tree, ARCHIVE_PATH) is False


def test_the_record_count_is_not_recounted_until_the_index_changes(site_tree: Settings) -> None:
    """Counting means reading the whole file, off an array, for a number that
    cannot change until a capture rewrites it."""
    make_capture(site_tree, "20260810T120000Z-full-wget", [(URL, "2026-08-10T12:00:00Z", b"hi")])
    replay.build_index(site_tree, ARCHIVE_PATH)

    first, _ = replay.index_stats(site_tree, ARCHIVE_PATH)

    path = replay.index_path(site_tree, ARCHIVE_PATH)
    original = path.read_bytes()
    # Rewrite with different content but the same size and mtime: the cache is
    # keyed on both, so this is the one edit it is entitled to miss.
    stat = path.stat()
    path.write_bytes(original)
    import os

    os.utime(path, (stat.st_atime, stat.st_mtime))
    assert replay.index_stats(site_tree, ARCHIVE_PATH)[0] == first

    # A real reindex changes the size, and the count is taken again.
    make_capture(site_tree, "20260811T120000Z-full-wget", [(POST, "2026-08-11T12:00:00Z", b"x")])
    replay.build_index(site_tree, ARCHIVE_PATH)
    assert replay.index_stats(site_tree, ARCHIVE_PATH)[0] > first


# ── keeping a recorded URL out of replay ─────────────────────────────────


def _cdxj(url: str, ts: str = "20260816120000") -> str:
    """One index line in the shape cdxj-indexer writes."""
    import json as _json

    key = "com,example)/" + url.split("/", 3)[-1] if "/" in url else "com,example)/"
    return f"{key} {ts} {_json.dumps({'url': url, 'mime': 'text/html', 'status': '200'})}\n"


def test_withholding_drops_only_the_matching_records() -> None:
    from cairn.services.replay import _without

    lines = [
        _cdxj("https://blog.example/2019/11/post.html"),
        _cdxj("https://www.blogger.com/interstitial/blog?u=https://blog.example/"),
        _cdxj("https://blog.example/2020/01/other.html"),
        _cdxj("https://draft.blogger.com/interstitial/blog?u=https://blog.example/"),
    ]

    keep, dropped = _without(lines, [r"/interstitial/"])

    assert dropped == 2
    assert len(keep) == 2
    assert all("interstitial" not in line for line in keep)


def test_the_pattern_is_matched_against_the_url_not_the_surt_key() -> None:
    """The SURT reverses the host and folds case, so a pattern somebody wrote
    against the URL they saw in a fetch list would match it only by accident.
    A rule that silently never fires is worse than no rule."""
    from cairn.services.replay import _without

    line = _cdxj("https://www.blogger.com/interstitial/blog?u=https://blog.example/")
    assert line.startswith("com,example)"), "the key really is reversed"

    _keep, dropped = _without([line], [r"^https://www\.blogger\.com/interstitial/"])
    assert dropped == 1, "anchored at the start of the real URL, which the key is not"


def test_an_unusable_pattern_does_not_cost_the_site_its_index() -> None:
    """These come out of a scope somebody typed into."""
    from cairn.services.replay import _without

    lines = [_cdxj("https://blog.example/a.html"), _cdxj("https://blog.example/b.html")]
    keep, dropped = _without(lines, ["(unclosed", r"/b\.html"])
    assert dropped == 1
    assert len(keep) == 1


def test_no_patterns_means_every_record_is_served(settings: Settings, tmp_path: Path) -> None:
    from cairn.services.replay import _without

    lines = [_cdxj("https://blog.example/a.html")]
    assert _without(lines, []) == (lines, 0)


def test_the_export_is_not_filtered_only_replay_is() -> None:
    """A WACZ is the archive; withholding is a statement about this instance's
    replay. `cdxj_lines` is what the packager shares, and it must stay
    complete — so the filter lives in `build_index` alone."""
    import inspect

    from cairn.services import replay as replay_service

    source = inspect.getsource(replay_service.cdxj_lines)
    assert "_without" not in source
    assert "withhold" not in source
