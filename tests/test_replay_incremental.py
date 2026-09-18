"""A replay index brought up to date after a capture, not rebuilt.

The standard every test here holds an update to is a rebuild: whatever an
update writes has to be the file a rebuild of the same tree writes, byte for
byte. Anything else is a second answer to "what is in these WARCs", and the
two would drift — which is the whole case the index used to make for always
rebuilding.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cairn.config import Settings
from cairn.db.models import Capture, Site
from cairn.db.types import utcnow
from cairn.services import replay, storage
from tests.conftest import XHR
from tests.test_replay import ARCHIVE_PATH, URL, make_capture, write_warc

POST_RE = r"/p/2\b"
BROKEN_GZIP = bytes.fromhex("1f8b0800000000000000") + b"junk"


@pytest.fixture
def site_tree(settings: Settings) -> Settings:
    storage.ensure_site_dirs(settings, ARCHIVE_PATH)
    return settings


def dir_name(n: int) -> str:
    return f"202608{10 + n:02d}T120000Z-feed-wget"


def add_capture(settings: Settings, n: int, *, body: bytes = b"") -> Path:
    """A capture holding its own post, and a fresh copy of the front page."""
    when = f"2026-08-{10 + n:02d}T12:00:00Z"
    return make_capture(
        settings,
        dir_name(n),
        [
            (f"{URL}p/{n}", when, body or f"post {n}".encode()),
            (URL, when, f"home as of capture {n}".encode()),
        ],
    )


def update(settings: Settings, withhold: list[str] | None = None) -> replay.IndexResult:
    return replay.update_index(settings, ARCHIVE_PATH, withhold=withhold)


def as_rebuilt(settings: Settings, withhold: list[str] | None = None) -> bytes:
    """What a rebuild of the same tree writes. Replaces the index under test,
    so it is always the last thing a test asks for."""
    return replay.build_index(settings, ARCHIVE_PATH, withhold=withhold).path.read_bytes()


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Which WARCs the indexer was asked to read, by file name."""
    seen: list[str] = []
    real = replay.cdxj_lines

    def spy(site_root: Path, warcs: list[Path]) -> list[str]:
        seen.extend(str(w.relative_to(site_root)).replace("\\", "/") for w in warcs)
        return real(site_root, warcs)

    monkeypatch.setattr(replay, "cdxj_lines", spy)
    return seen


def index_file(settings: Settings) -> Path:
    return replay.index_path(settings, ARCHIVE_PATH)


def state_file(settings: Settings) -> Path:
    return index_file(settings).with_name(replay.INDEX_STATE_FILE)


def edit_state(settings: Settings, change: Callable[[dict[str, Any]], None]) -> None:
    path = state_file(settings)
    state = json.loads(path.read_text(encoding="utf-8"))
    change(state)
    path.write_text(json.dumps(state), encoding="utf-8")


def warc_name(n: int) -> str:
    return f"captures/{dir_name(n)}/warc/part-00000.warc.gz"


# ── the standard ─────────────────────────────────────────────────────────


def test_an_update_writes_what_a_rebuild_writes(site_tree: Settings) -> None:
    add_capture(site_tree, 1)
    update(site_tree)
    add_capture(site_tree, 2)
    add_capture(site_tree, 3)
    result = update(site_tree)

    written = result.path.read_bytes()
    assert result.rebuilt is None
    assert result.records == len(written.splitlines()) == 6
    assert result.warcs == 3
    assert written == as_rebuilt(site_tree)


def test_every_version_of_a_page_survives_an_update(site_tree: Settings) -> None:
    """The time dimension is the reason the index spans captures at all."""
    add_capture(site_tree, 1)
    update(site_tree)
    add_capture(site_tree, 2)
    update(site_tree)
    versions = replay.lookup(site_tree, ARCHIVE_PATH, URL)
    assert [v.timestamp for v in versions] == ["20260811120000", "20260812120000"]


# ── reading only what is new ─────────────────────────────────────────────


def test_only_the_new_capture_is_read(site_tree: Settings, reads: list[str]) -> None:
    add_capture(site_tree, 1)
    add_capture(site_tree, 2)
    update(site_tree)
    reads.clear()

    add_capture(site_tree, 3)
    result = update(site_tree)

    assert reads == [warc_name(3)]
    assert result.read == 1
    assert result.rebuilt is None


def test_nothing_new_reads_nothing_and_writes_nothing(
    site_tree: Settings, reads: list[str]
) -> None:
    add_capture(site_tree, 1)
    update(site_tree)
    reads.clear()
    before = index_file(site_tree).stat().st_mtime_ns

    result = update(site_tree)

    assert reads == []
    assert result.read == 0
    assert result.records == 2
    assert index_file(site_tree).stat().st_mtime_ns == before


def test_a_capture_gone_from_disk_drops_out_without_reading_anything(
    site_tree: Settings, reads: list[str]
) -> None:
    """Deleted over the share, say — nothing told the app."""
    add_capture(site_tree, 1)
    add_capture(site_tree, 2)
    update(site_tree)
    reads.clear()

    shutil.rmtree(storage.site_dir(site_tree, ARCHIVE_PATH) / "captures" / dir_name(2))
    result = update(site_tree)

    assert reads == []
    assert result.records == 2
    assert "p/2" not in result.path.read_text(encoding="utf-8")
    assert result.path.read_bytes() == as_rebuilt(site_tree)


def test_a_warc_that_changed_is_read_again(site_tree: Settings, reads: list[str]) -> None:
    """A WARC indexed while it was still being written — Rebuild index pressed
    mid-crawl — is recorded at the size it had, and read again once it has
    grown."""
    warc = add_capture(site_tree, 1)
    add_capture(site_tree, 2)
    update(site_tree)
    reads.clear()

    write_warc(
        warc,
        [
            (f"{URL}p/1", "2026-08-11T12:00:00Z", b"post 1"),
            (f"{URL}p/1b", "2026-08-11T12:00:05Z", b"a page that arrived later"),
        ],
    )
    result = update(site_tree)

    assert reads == [warc_name(1)]
    text = result.path.read_text(encoding="utf-8")
    assert "p/1b" in text
    assert "home as of capture 1" not in text  # the old lines went with the old file
    assert result.path.read_bytes() == as_rebuilt(site_tree)


def test_a_warc_that_cannot_be_read_leaves_the_index_as_it_was(
    site_tree: Settings, reads: list[str]
) -> None:
    """Same policy as a rebuild: all of it or none of it, and the next update
    starts from a record that still describes the file."""
    add_capture(site_tree, 1)
    update(site_tree)
    before = index_file(site_tree).read_bytes()
    record = state_file(site_tree).read_bytes()

    broken = storage.ensure_capture_dirs(site_tree, ARCHIVE_PATH, dir_name(2)) / "warc"
    # A gzip header over data that is not deflate. Plain junk is not enough:
    # the indexer reads that as one nonsense record rather than refusing it.
    (broken / "part-00000.warc.gz").write_bytes(BROKEN_GZIP)
    with pytest.raises(replay.ReplayError, match=dir_name(2)):
        update(site_tree)

    assert index_file(site_tree).read_bytes() == before
    assert state_file(site_tree).read_bytes() == record
    shutil.rmtree(broken.parent)
    reads.clear()
    assert update(site_tree).read == 0
    assert reads == []


# ── when an update is not trusted ────────────────────────────────────────


def test_changed_skip_patterns_mean_a_rebuild(site_tree: Settings, reads: list[str]) -> None:
    """Withheld records are not in the index, so bringing one back means reading
    the WARC it lives in — and every WARC might hold one."""
    add_capture(site_tree, 1)
    add_capture(site_tree, 2)
    update(site_tree)
    reads.clear()

    result = update(site_tree, withhold=[POST_RE])

    assert result.rebuilt == "the skip patterns changed"
    assert len(reads) == 2
    assert result.withheld == 1
    assert result.path.read_bytes() == as_rebuilt(site_tree, withhold=[POST_RE])


def test_the_same_patterns_in_another_order_are_not_a_change(
    site_tree: Settings, reads: list[str]
) -> None:
    add_capture(site_tree, 1)
    update(site_tree, withhold=["a", POST_RE])
    reads.clear()
    result = update(site_tree, withhold=[POST_RE, "a", POST_RE])
    assert result.rebuilt is None
    assert reads == []


def test_withheld_records_are_counted_across_updates(site_tree: Settings) -> None:
    add_capture(site_tree, 1)
    update(site_tree, withhold=[POST_RE])
    add_capture(site_tree, 2)
    result = update(site_tree, withhold=[POST_RE])
    assert result.rebuilt is None
    assert result.withheld == 1
    assert result.records == 3


def test_no_record_means_a_rebuild(site_tree: Settings) -> None:
    """Every site's first update after this shipped."""
    add_capture(site_tree, 1)
    replay.build_index(site_tree, ARCHIVE_PATH)
    state_file(site_tree).unlink()

    result = update(site_tree)

    assert result.rebuilt == "nothing recorded which WARCs the index holds"
    assert state_file(site_tree).is_file()
    assert update(site_tree).rebuilt is None


def test_an_index_rewritten_behind_its_back_means_a_rebuild(site_tree: Settings) -> None:
    """Whatever did it — an older version's rebuild, a hand edit, a crash
    between writing the index and writing its record — the record no longer
    describes the file, and merging into it would be merging into a guess."""
    add_capture(site_tree, 1)
    update(site_tree)
    with open(index_file(site_tree), "ab") as fh:
        fh.write(b'zz,stray)/ 20260101000000 {"url": "https://stray/", "filename": "x"}\n')

    result = update(site_tree)

    assert result.rebuilt == "the index was rewritten by something that did not record it"
    assert b"stray" not in result.path.read_bytes()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda s: s.update(format=0), "the index format changed"),
        (lambda s: s.update(indexer="0.0.0"), "cdxj-indexer changed"),
        (lambda s: s.update(index=None), "the record does not describe an index"),
        (lambda s: s["warcs"].update(x={"size": 1}), "the record of indexed WARCs is unreadable"),
    ],
)
def test_a_record_it_cannot_vouch_for_means_a_rebuild(
    site_tree: Settings, change: Callable[[dict[str, Any]], Any], reason: str
) -> None:
    add_capture(site_tree, 1)
    update(site_tree)
    edit_state(site_tree, change)
    assert update(site_tree).rebuilt == reason


def test_the_index_format_was_bumped_for_post_keys() -> None:
    """A pin, not a tautology: this number is the only thing that sends an
    index written by an older Cairn back to its WARCs.

    Format 1 indexed a POST under its plain URL, which is not the key pywb
    looks one up by, so the record was there and unreachable. Nothing about
    the WARCs changes when Cairn is upgraded, so without the bump an update
    would keep those lines forever.
    """
    assert replay.INDEX_FORMAT == 2


def test_an_index_from_the_previous_format_is_rebuilt(site_tree: Settings) -> None:
    add_capture(site_tree, 1)
    update(site_tree)
    edit_state(site_tree, lambda s: s.update(format=replay.INDEX_FORMAT - 1))

    assert update(site_tree).rebuilt == "the index format changed"


def test_a_record_that_is_not_json_means_a_rebuild(site_tree: Settings) -> None:
    add_capture(site_tree, 1)
    update(site_tree)
    state_file(site_tree).write_text("{ not json", encoding="utf-8")
    assert update(site_tree).rebuilt == "nothing recorded which WARCs the index holds"


def test_no_index_at_all_means_a_rebuild(site_tree: Settings) -> None:
    add_capture(site_tree, 1)
    update(site_tree)
    index_file(site_tree).unlink()
    result = update(site_tree)
    assert result.rebuilt == "there was no index"
    assert result.records == 2


def test_an_index_that_failed_to_write_leaves_no_record_claiming_it(
    site_tree: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record is written after the index, never before. The other order
    leaves a record naming WARCs the file on disk does not hold, and every
    update after it would trust that — records missing from replay, silently,
    for good."""
    add_capture(site_tree, 1)
    update(site_tree)
    record = state_file(site_tree).read_bytes()
    add_capture(site_tree, 2)

    real_writer = storage.atomic_writer
    real_write = storage.write_atomic

    def failing_writer(path: Path) -> Any:
        if path == index_file(site_tree):
            raise OSError("disk full")
        return real_writer(path)

    def failing_write(path: Path, data: Any, **kwargs: Any) -> None:
        if path == index_file(site_tree):
            raise OSError("disk full")
        real_write(path, data, **kwargs)

    monkeypatch.setattr(storage, "atomic_writer", failing_writer)
    monkeypatch.setattr(storage, "write_atomic", failing_write)

    with pytest.raises(OSError):
        update(site_tree)  # an update
    assert state_file(site_tree).read_bytes() == record

    state_file(site_tree).unlink()
    with pytest.raises(OSError):
        update(site_tree)  # and a rebuild
    assert not state_file(site_tree).exists()


def test_the_record_is_invisible_to_pywb(site_tree: Settings) -> None:
    """pywb 2.9.1 loads every file in an index directory whose name ends in one
    of these; anything else it lists and ignores."""
    pywb_index_extensions = (".cdx", ".cdxj", ".idx", ".summary")
    add_capture(site_tree, 1)
    update(site_tree)
    assert state_file(site_tree).parent == index_file(site_tree).parent
    assert not replay.INDEX_STATE_FILE.endswith(pywb_index_extensions)
    indexes = [p.name for p in index_file(site_tree).parent.iterdir()]
    assert [n for n in indexes if n.endswith(pywb_index_extensions)] == [replay.INDEX_FILE]


def test_one_writer_at_a_time(site_tree: Settings) -> None:
    """Post-processing and Rebuild index can run at once, and an update that
    read the index while a rebuild replaced it would merge into a file that
    is gone."""
    add_capture(site_tree, 1)
    finished = threading.Event()

    def run() -> None:
        update(site_tree)
        finished.set()

    with replay._exclusive(index_file(site_tree)):
        worker = threading.Thread(target=run)
        worker.start()
        time.sleep(0.3)
        assert not finished.is_set()
    worker.join(10)
    assert finished.is_set()


# ── forgetting a capture ─────────────────────────────────────────────────


def test_forgetting_a_capture_reads_nothing_and_matches_a_rebuild(
    site_tree: Settings, reads: list[str]
) -> None:
    add_capture(site_tree, 1)
    add_capture(site_tree, 2)
    add_capture(site_tree, 3)
    update(site_tree)
    reads.clear()

    shutil.rmtree(storage.site_dir(site_tree, ARCHIVE_PATH) / "captures" / dir_name(2))
    dropped = replay.forget_capture(site_tree, ARCHIVE_PATH, dir_name(2))

    assert dropped == 2
    assert reads == []
    # And the record still vouches for the file, so the next capture updates.
    after = update(site_tree)
    assert after.rebuilt is None
    assert after.read == 0
    assert after.path.read_bytes() == as_rebuilt(site_tree)


def test_forgetting_leaves_a_capture_whose_name_starts_the_same(site_tree: Settings) -> None:
    add_capture(site_tree, 1)
    longer = f"{dir_name(1)}x"
    make_capture(site_tree, longer, [(f"{URL}other", "2026-08-11T12:00:09Z", b"other")])
    update(site_tree)

    assert replay.forget_capture(site_tree, ARCHIVE_PATH, dir_name(1)) == 2
    assert f"captures/{longer}/" in index_file(site_tree).read_text(encoding="utf-8")


def test_forgetting_without_a_record_still_forgets(site_tree: Settings) -> None:
    add_capture(site_tree, 1)
    add_capture(site_tree, 2)
    replay.build_index(site_tree, ARCHIVE_PATH)
    state_file(site_tree).unlink()

    assert replay.forget_capture(site_tree, ARCHIVE_PATH, dir_name(2)) == 2
    assert "p/2" not in index_file(site_tree).read_text(encoding="utf-8")


def test_forgetting_with_no_index_is_nothing(site_tree: Settings) -> None:
    assert replay.forget_capture(site_tree, ARCHIVE_PATH, dir_name(1)) == 0


def test_deleting_a_capture_takes_it_out_of_replay(authed: TestClient) -> None:
    """docs/09 said a delete triggered a reindex. It did not, and the index
    went on naming the capture's files until the site's next capture."""
    factory = authed.app.state.sessionmaker  # type: ignore[attr-defined]
    settings: Settings = authed.app.state.settings  # type: ignore[attr-defined]
    created = authed.post("/api/sites", json={"seed_url": URL}, headers=XHR)
    assert created.status_code == 201, created.text

    with factory() as s:
        site = s.get(Site, created.json()["id"])
        assert site is not None
        archive_path = site.archive_path
        ids = []
        for n in (1, 2):
            out = storage.ensure_capture_dirs(settings, archive_path, dir_name(n))
            write_warc(
                out / storage.WARC_DIR / "part-00000.warc.gz",
                [(URL, f"2026-08-{10 + n:02d}T12:00:00Z", f"capture {n}".encode())],
            )
            capture = Capture(
                site_id=site.id,
                kind="feed",
                engine_id=site.engine_id,
                dir_name=dir_name(n),
                started_at=utcnow(),
                status="ok",
            )
            s.add(capture)
            s.flush()
            ids.append(capture.id)
        s.commit()
    replay.update_index(settings, archive_path)
    assert len(replay.lookup(settings, archive_path, URL)) == 2

    res = authed.delete(f"/api/captures/{ids[1]}", headers=XHR)
    assert res.status_code == 200, res.text

    versions = replay.lookup(settings, archive_path, URL)
    assert [v.timestamp for v in versions] == ["20260811120000"]


# ── post-processing ──────────────────────────────────────────────────────


def test_post_processing_reads_only_its_own_capture(
    db: Any, settings: Settings, reads: list[str]
) -> None:
    from cairn.services import postprocess
    from cairn.services import sites as site_service

    site = site_service.create_site(db, settings, seed_url=URL)
    db.flush()

    def run() -> dict[str, Any]:
        capture = Capture(
            site_id=site.id,
            kind="feed",
            engine_id=site.engine_id,
            dir_name="x",
            started_at=utcnow(),
            status="ok",
        )
        ctx = postprocess.Context(
            session=db,
            settings=settings,
            capture=capture,
            site=site,
            output_dir=Path("."),
            tool_version=None,
            stats={},
            scope={},
            seeds=[],
            seed_source={},
            artifacts=[],
            warnings=[],
        )
        postprocess.step_cdxj_index(ctx)
        return ctx.stats

    def capture_on_disk(n: int) -> None:
        out = storage.ensure_capture_dirs(settings, site.archive_path, dir_name(n))
        write_warc(
            out / storage.WARC_DIR / "part-00000.warc.gz",
            [(f"{URL}p/{n}", f"2026-08-{10 + n:02d}T12:00:00Z", b"post")],
        )

    capture_on_disk(1)
    first = run()
    assert first["index_warcs_read"] == 1
    assert first["index_rebuilt"] == "there was no index"

    capture_on_disk(2)
    reads.clear()
    second = run()
    assert second["index_warcs_read"] == 1
    assert "index_rebuilt" not in second
    assert reads == [warc_name(2)]
    assert second["index_records"] == 2
