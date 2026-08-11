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
