"""Archive integrity verification.

The thing being defended against is silent: a WARC nobody has opened since
2026 whose bytes stopped matching what was written. Every test here therefore
damages a real file and checks the pass notices — a verifier that only ever
sees intact archives has never been tested.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, Site
from cairn.db.types import utcnow
from cairn.services import integrity, postprocess, replay, storage
from tests.test_search import blogger_page, write_warc

MARKER = "the machair in flower on Harris"


def build_site(db: Session, settings: Settings, *, slug: str = "coast") -> tuple[Site, Capture]:
    site = Site(
        folder_id=1,
        slug=slug,
        title="Coast & Light",
        seed_url="http://blog.test/",
        primary_host="blog.test",
        archive_path=f"Unfiled/{slug}",
    )
    db.add(site)
    db.flush()
    capture = Capture(
        site_id=site.id,
        kind="full",
        engine_id="wget-warc",
        dir_name="20260811T090000Z-full-wget",
        status="ok",
        started_at=utcnow(),
    )
    db.add(capture)
    db.flush()

    storage.ensure_site_dirs(settings, site.archive_path)
    capture_dir = storage.ensure_capture_dirs(settings, site.archive_path, capture.dir_name)
    warc = capture_dir / storage.WARC_DIR / "part-00000.warc.gz"
    write_warc(
        warc,
        {
            "http://blog.test/": blogger_page("index", "Index", "Photographs from the west."),
            "http://blog.test/harris.html": blogger_page("harris", "Harris", MARKER),
        },
    )

    # What the checksum post-processor records, computed the same way.
    artifacts = [
        {
            "name": f"{storage.WARC_DIR}/{warc.name}",
            "kind": "warc",
            "size": warc.stat().st_size,
            "sha256": postprocess.sha256_file(warc),
        }
    ]
    capture.warc_files = artifacts
    db.flush()
    storage.write_json(
        capture_dir / storage.MANIFEST_FILE,
        storage.build_manifest(
            capture_id=capture.id,
            site_slug=site.slug,
            kind="full",
            engine_id="wget-warc",
            engine_version="1",
            tool_version=None,
            started_at=capture.started_at,
            finished_at=utcnow(),
            status="ok",
            seeds=[site.seed_url],
            seed_source={"manual": 1},
            scope={},
            stats={},
            warc_files=artifacts,
        ),
    )
    return site, capture


def warc_of(settings: Settings, site: Site, capture: Capture) -> Path:
    return (
        storage.site_dir(settings, site.archive_path)
        / storage.CAPTURES_DIR
        / capture.dir_name
        / storage.WARC_DIR
        / "part-00000.warc.gz"
    )


# ── the intact case ──────────────────────────────────────────────────────


def test_an_intact_archive_verifies(db: Session, settings: Settings) -> None:
    build_site(db, settings)
    report = integrity.verify(db, settings)

    assert report.ok, [f.to_dict() for f in report.findings]
    assert report.captures == 1
    assert report.files == 1
    assert report.bytes_read > 0


def test_progress_is_reported_per_capture(db: Session, settings: Settings) -> None:
    build_site(db, settings)
    seen: list[tuple[int, int, str]] = []
    integrity.verify(db, settings, progress=lambda a, b, c: seen.append((a, b, c)))
    assert seen and seen[0][1] == 1


# ── the cases it exists for ──────────────────────────────────────────────


def test_a_flipped_byte_is_found(db: Session, settings: Settings) -> None:
    """Bit rot on an array is the whole reason for this job. The file is still
    there, still the right size, and no longer the thing that was written."""
    site, capture = build_site(db, settings)
    warc = warc_of(settings, site, capture)
    raw = bytearray(warc.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    warc.write_bytes(bytes(raw))

    report = integrity.verify(db, settings)
    assert not report.ok
    kinds = {f.kind for f in report.findings}
    assert kinds == {"mismatch"}
    assert "backup" in report.findings[0].detail


def test_a_missing_file_is_found(db: Session, settings: Settings) -> None:
    site, capture = build_site(db, settings)
    warc_of(settings, site, capture).unlink()

    report = integrity.verify(db, settings)
    assert [f.kind for f in report.findings] == ["missing"]


def test_a_missing_capture_directory_is_found(db: Session, settings: Settings) -> None:
    import shutil

    site, capture = build_site(db, settings)
    shutil.rmtree(
        storage.site_dir(settings, site.archive_path) / storage.CAPTURES_DIR / capture.dir_name
    )

    report = integrity.verify(db, settings)
    assert [f.kind for f in report.findings] == ["missing"]


def test_truncation_is_only_found_by_the_deep_pass(db: Session, settings: Settings) -> None:
    """The case a checksum cannot cover: a WARC that was already broken when
    its checksum was taken, because the container stopped mid-write.

    warcio alone does not catch this — measured: a four-record file missing
    its last forty bytes parses all four and reports nothing — so the deep
    pass also insists the compressed stream ends properly.
    """
    site, capture = build_site(db, settings)
    warc = warc_of(settings, site, capture)
    truncated = warc.read_bytes()[:-40]
    warc.write_bytes(truncated)
    # Re-record the checksum, so only parsing can tell anything is wrong.
    capture.warc_files = [
        {
            "name": f"{storage.WARC_DIR}/{warc.name}",
            "kind": "warc",
            "size": warc.stat().st_size,
            "sha256": postprocess.sha256_file(warc),
        }
    ]
    db.flush()
    (
        storage.site_dir(settings, site.archive_path)
        / storage.CAPTURES_DIR
        / capture.dir_name
        / storage.MANIFEST_FILE
    ).unlink()

    assert integrity.verify(db, settings).ok
    deep = integrity.verify(db, settings, deep=True)
    assert not deep.ok
    assert [f.kind for f in deep.findings] == ["unreadable"]


def test_an_index_naming_a_deleted_warc_is_reported(db: Session, settings: Settings) -> None:
    """Not corruption, and it looks exactly like data loss to whoever meets
    it: replay 503s for pages that are still on disk."""
    site, _capture = build_site(db, settings)
    replay.build_index(settings, site.archive_path)
    index = replay.index_path(settings, site.archive_path)
    assert index.stat().st_size > 0

    text = index.read_text(encoding="utf-8").replace("part-00000.warc.gz", "part-99999.warc.gz")
    index.write_text(text, encoding="utf-8")

    report = integrity.verify(db, settings)
    assert [f.kind for f in report.findings] == ["stale-index"]
    assert "Rebuild the index" in report.findings[0].detail


def test_a_capture_with_no_recorded_checksums_says_so(db: Session, settings: Settings) -> None:
    """Silence would be worse: a capture nothing can check reads as verified."""
    site, capture = build_site(db, settings)
    capture.warc_files = []
    db.flush()
    (
        storage.site_dir(settings, site.archive_path)
        / storage.CAPTURES_DIR
        / capture.dir_name
        / storage.MANIFEST_FILE
    ).unlink()

    report = integrity.verify(db, settings)
    assert [f.kind for f in report.findings] == ["no-checksums"]


def test_the_manifest_is_believed_before_the_database(db: Session, settings: Settings) -> None:
    """The manifest travels with the archive. After a database rebuild, the
    rows are reconstructed from the same disk being verified — checking
    against them would be checking the disk against itself."""
    _site, capture = build_site(db, settings)
    capture.warc_files = [
        {"name": "warc/part-00000.warc.gz", "kind": "warc", "size": 1, "sha256": "0" * 64}
    ]
    db.flush()

    assert integrity.verify(db, settings).ok


def test_a_trashed_site_is_not_verified(db: Session, settings: Settings) -> None:
    site, capture = build_site(db, settings)
    warc_of(settings, site, capture).unlink()
    site.deleted_at = utcnow()
    db.flush()

    assert integrity.verify(db, settings).ok


def test_verification_can_be_scoped_to_one_site(db: Session, settings: Settings) -> None:
    first, capture = build_site(db, settings, slug="coast")
    second, _ = build_site(db, settings, slug="other")
    warc_of(settings, first, capture).unlink()

    assert integrity.verify(db, settings, site_id=second.id).ok
    assert not integrity.verify(db, settings, site_id=first.id).ok


# ── the report and the schedule ──────────────────────────────────────────


def test_the_report_survives_a_restart(db: Session, settings: Settings) -> None:
    build_site(db, settings)
    report = integrity.verify(db, settings)
    integrity.save(settings, report)

    loaded = integrity.load(settings)
    assert loaded is not None
    assert loaded["ok"] is True
    assert loaded["captures"] == 1


def test_health_names_the_oldest_unverified_capture(db: Session, settings: Settings) -> None:
    """The number worth watching. A pass that never reaches half the archive
    reports success on the half it does reach."""
    _site, capture = build_site(db, settings)
    health = integrity.health(db, settings)
    assert health["captures"] == 1
    assert health["verified"] == 0
    assert health["oldest_unverified"]["dir_name"] == capture.dir_name
    assert health["due"] is True

    integrity.save(settings, integrity.verify(db, settings))
    health = integrity.health(db, settings)
    assert health["verified"] == 1
    assert health["oldest_unverified"] is None
    assert health["due"] is False


def test_a_capture_with_a_finding_does_not_count_as_verified(
    db: Session, settings: Settings
) -> None:
    site, capture = build_site(db, settings)
    warc_of(settings, site, capture).unlink()
    integrity.save(settings, integrity.verify(db, settings))

    health = integrity.health(db, settings)
    assert health["verified"] == 0
    assert health["oldest_unverified"]["dir_name"] == capture.dir_name


def test_verification_can_be_switched_off(db: Session, settings: Settings) -> None:
    from cairn.services import settings_store

    build_site(db, settings)
    settings_store.put(db, integrity.INTERVAL_SETTING, 0)
    assert integrity.due(db, settings) is False
