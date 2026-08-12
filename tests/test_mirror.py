"""Checking a copy of the archive.

Not a sync. Making the copy is rsync's job or restic's, and both are years
ahead of anything worth writing here; what this instance has and they do not is
the checksum taken when each file was written. So the feature is the question
they cannot answer — is the copy complete, and are its bytes still the bytes —
and these tests are about the two ways of getting that wrong: reporting a
missing capture as fine, and reporting the live archive as if it were the copy.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, Site
from cairn.services import integrity, mirror, storage
from cairn.services import sites as site_service
from tests.conftest import XHR

WARC = b"WARC/1.0\r\nWARC-Type: warcinfo\r\nContent-Length: 0\r\n\r\n\r\n\r\n"


def _capture(db: Session, settings: Settings, site: Site, dir_name: str) -> Capture:
    """One capture with a real file and a manifest that records its hash."""
    root = storage.site_dir(settings, site.archive_path) / storage.CAPTURES_DIR / dir_name
    (root / storage.WARC_DIR).mkdir(parents=True, exist_ok=True)
    warc = root / storage.WARC_DIR / "part-00000.warc.gz"
    warc.write_bytes(WARC)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "warc_files": [
                    {
                        "name": "warc/part-00000.warc.gz",
                        "sha256": hashlib.sha256(WARC).hexdigest(),
                        "bytes": len(WARC),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    capture = Capture(
        site_id=site.id, kind="full", engine_id="wget-warc", dir_name=dir_name, status="ok"
    )
    db.add(capture)
    db.flush()
    return capture


def _archive(db: Session, settings: Settings) -> Site:
    site = site_service.create_site(db, settings, seed_url="https://copy.example.com/")
    _capture(db, settings, site, "20260101-000000-full")
    _capture(db, settings, site, "20260201-000000-full")
    return site


def _copy_to(settings: Settings, target: Path, *, skip: str | None = None) -> Path:
    """A copy of the data directory, optionally with one capture left out."""
    import shutil

    target.mkdir(parents=True, exist_ok=True)
    relative = settings.archives_dir.relative_to(settings.data_dir)
    shutil.copytree(
        settings.archives_dir,
        target / relative,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(skip) if skip else None,
    )
    return target


# ── the listing ──────────────────────────────────────────────────────────


def test_a_complete_copy_reads_as_complete(db: Session, settings: Settings, tmp_path: Path) -> None:
    _archive(db, settings)
    root = _copy_to(settings, tmp_path / "backup")

    found = mirror.survey(db, settings, root)
    assert found.captures == 2
    assert found.present == 2
    assert found.complete


def test_a_sync_that_skipped_a_directory_is_caught(
    db: Session, settings: Settings, tmp_path: Path
) -> None:
    """The common failure, and the one a sync reports success for."""
    _archive(db, settings)
    root = _copy_to(settings, tmp_path / "backup", skip="20260201-000000-full")

    found = mirror.survey(db, settings, root)
    assert found.captures == 2
    assert found.present == 1
    assert not found.complete
    assert found.sites[0].missing == ["20260201-000000-full"]


def test_an_empty_directory_is_not_mistaken_for_a_backup(
    db: Session, settings: Settings, tmp_path: Path
) -> None:
    _archive(db, settings)
    empty = tmp_path / "nothing"
    empty.mkdir()
    found = mirror.survey(db, settings, empty)
    assert found.present == 0
    assert not found.complete


def test_a_capture_directory_with_no_warc_does_not_count(
    db: Session, settings: Settings, tmp_path: Path
) -> None:
    """An interrupted copy leaves the directory and not the bytes."""
    site = _archive(db, settings)
    root = _copy_to(settings, tmp_path / "backup")
    warc = (
        storage.site_dir_under(root, settings, site.archive_path)
        / storage.CAPTURES_DIR
        / "20260101-000000-full"
        / storage.WARC_DIR
        / "part-00000.warc.gz"
    )
    warc.unlink()

    found = mirror.survey(db, settings, root)
    assert found.present == 1
    assert "20260101-000000-full" in found.sites[0].missing


def test_sites_the_copy_has_and_we_do_not_are_reported_not_failed(
    db: Session, settings: Settings, tmp_path: Path
) -> None:
    """An old backup holds sites since deleted, which is often the point."""
    _archive(db, settings)
    root = _copy_to(settings, tmp_path / "backup")
    stranger = root / settings.archives_dir.relative_to(settings.data_dir) / "someone-else"
    stranger.mkdir(parents=True)
    (stranger / "site.yaml").write_text("id: 99\n", encoding="utf-8")

    found = mirror.survey(db, settings, root)
    assert found.complete
    assert any("someone-else" in name for name in found.unknown_dirs)


# ── the path itself ──────────────────────────────────────────────────────


def test_checking_the_archive_against_itself_is_refused(settings: Settings) -> None:
    """It would pass, and mean nothing."""
    settings.archives_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(mirror.MirrorError, match="own data directory"):
        mirror.require_root(settings, str(settings.data_dir))
    with pytest.raises(mirror.MirrorError, match="own data directory"):
        mirror.require_root(settings, str(settings.archives_dir))


def test_a_relative_or_missing_path_is_refused(settings: Settings) -> None:
    """Two ways to point at nothing, both worth their own sentence."""
    with pytest.raises(mirror.MirrorError, match="absolute"):
        mirror.require_root(settings, "backup")
    with pytest.raises(mirror.MirrorError, match="not a directory"):
        mirror.require_root(settings, str(settings.data_dir.parent / "no-such-backup"))


# ── the checksum pass ────────────────────────────────────────────────────


def test_the_copy_is_checked_against_what_was_recorded_here(
    db: Session, settings: Settings, tmp_path: Path
) -> None:
    _archive(db, settings)
    root = _copy_to(settings, tmp_path / "backup")

    report = integrity.verify(db, settings, root=root)
    assert report.ok, [f.to_dict() for f in report.findings]
    assert report.captures == 2
    assert report.root == str(root)


def test_a_byte_flipped_in_the_copy_is_found(
    db: Session, settings: Settings, tmp_path: Path
) -> None:
    """The question rsync cannot answer.

    It reports that it transferred bytes. Whether the bytes in the copy are
    still the bytes that were written is a fact only this instance holds, in
    the checksum the capture recorded.
    """
    site = _archive(db, settings)
    root = _copy_to(settings, tmp_path / "backup")
    warc = (
        storage.site_dir_under(root, settings, site.archive_path)
        / storage.CAPTURES_DIR
        / "20260101-000000-full"
        / storage.WARC_DIR
        / "part-00000.warc.gz"
    )
    data = bytearray(warc.read_bytes())
    data[10] ^= 0x01
    warc.write_bytes(bytes(data))

    report = integrity.verify(db, settings, root=root)
    assert not report.ok
    kinds = {f.kind for f in report.findings}
    assert "mismatch" in kinds
    # And the *live* archive is still fine, which is the whole distinction.
    assert integrity.verify(db, settings).ok


def test_a_mirror_report_is_not_the_archives_state(
    db: Session, settings: Settings, tmp_path: Path
) -> None:
    """It is a report about somewhere else and must not be saved as ours.

    Otherwise a bad backup would show up on the archive health page as a
    problem with the archive, and the next real verification would be the only
    thing that corrected it.
    """
    _archive(db, settings)
    root = _copy_to(settings, tmp_path / "backup")
    integrity.save(db and settings, integrity.verify(db, settings))
    before = integrity.load(settings)

    report = integrity.verify(db, settings, root=root)
    assert report.root
    assert integrity.load(settings) == before


def test_the_index_check_is_skipped_for_a_copy(
    db: Session, settings: Settings, tmp_path: Path
) -> None:
    """The replay index is derived and belongs to whoever serves replay.

    A copy that does not carry one is not a copy with a problem, and reporting
    it as `stale-index` would train the reader to ignore the finding that
    matters.
    """
    _archive(db, settings)
    root = _copy_to(settings, tmp_path / "backup")
    report = integrity.verify(db, settings, root=root)
    assert not any(f.kind == "stale-index" for f in report.findings)


# ── through the API ──────────────────────────────────────────────────────


def test_the_survey_and_the_job_round_trip(
    authed: TestClient, db: Session, settings: Settings, tmp_path: Path
) -> None:
    _archive(db, settings)
    db.commit()
    root = _copy_to(settings, tmp_path / "backup")

    listed = authed.get(f"/api/mirror?path={root}", headers=XHR)
    assert listed.status_code == 200, listed.text
    assert listed.json()["complete"] is True

    started = authed.post(f"/api/mirror/verify?path={root}", headers=XHR)
    assert started.status_code == 202, started.text
    assert started.json()["job_id"]


def test_the_api_refuses_a_path_inside_the_archive(authed: TestClient, settings: Settings) -> None:
    response = authed.get(f"/api/mirror?path={settings.data_dir}", headers=XHR)
    assert response.status_code == 400
    assert "own data directory" in response.json()["error"]["message"]
