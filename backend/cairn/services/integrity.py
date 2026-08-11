"""Archive integrity: re-checksumming what is on disk against what we recorded.

Archives are cold data. A WARC written in 2026 may not be opened until 2031,
and a NAS array will not tell you that a block went bad in a file nobody read.
The difference between noticing in a week and noticing never is a job that
reads the bytes back and compares them to the hash taken when they were
written — which is why the `checksum` post-processor computes that hash itself
rather than recording what the engine claimed.

**It never repairs.** There is nothing here that could: a WARC is immutable
and a bad one cannot be corrected, only re-captured. So the output is a report
that says exactly which file, which capture, and what the plausible next step
is, and every action it suggests is one somebody has to choose.

Four kinds of finding, in descending order of how much they should worry you:

  `missing`     the file the manifest names is not there at all
  `mismatch`    it is there and its bytes have changed
  `unreadable`  it parses as a WARC until it does not
  `stale-index` the replay index names a file that no longer exists
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, Site
from cairn.db.types import to_iso, utcnow
from cairn.logging import get_logger
from cairn.services import postprocess, storage

log = get_logger(__name__)

LAST_RUN_SETTING = "integrity.last_run"
INTERVAL_SETTING = "integrity.verify_days"
REPORT_FILE = "integrity.json"

SEVERITY = {"missing": 3, "mismatch": 3, "unreadable": 2, "stale-index": 1, "no-checksums": 1}
READ_CHUNK = 1024 * 1024


@dataclass(slots=True)
class Finding:
    kind: str
    site_id: int
    site_title: str
    capture_id: int | None
    capture_dir: str
    detail: str
    file: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "site_id": self.site_id,
            "site_title": self.site_title,
            "capture_id": self.capture_id,
            "capture_dir": self.capture_dir,
            "file": self.file,
            "detail": self.detail,
            "severity": SEVERITY.get(self.kind, 1),
        }


@dataclass(slots=True)
class Report:
    started_at: datetime
    finished_at: datetime | None = None
    sites: int = 0
    captures: int = 0
    files: int = 0
    bytes_read: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": to_iso(self.started_at),
            "finished_at": to_iso(self.finished_at) if self.finished_at else None,
            "sites": self.sites,
            "captures": self.captures,
            "files": self.files,
            "bytes_read": self.bytes_read,
            "ok": self.ok,
            "findings": [f.to_dict() for f in self.findings],
        }


def verify(
    session: Session,
    settings: Settings,
    *,
    site_id: int | None = None,
    deep: bool = False,
    progress: Callable[[int, int, str], None] | None = None,
) -> Report:
    """Walk the archive and check every recorded artifact.

    `deep` additionally parses each WARC end to end. That reads every byte
    twice — once to hash, once to parse — so it is the deliberate slow pass
    rather than what the weekly run does.
    """
    report = Report(started_at=utcnow())
    sites = list(
        session.scalars(
            select(Site)
            .where(Site.deleted_at.is_(None), *([Site.id == site_id] if site_id else []))
            .order_by(Site.id)
        ).all()
    )
    captures = _captures_for(session, [s.id for s in sites])
    total = sum(len(captures.get(s.id, [])) for s in sites)
    done = 0

    for site in sites:
        report.sites += 1
        for capture in captures.get(site.id, []):
            done += 1
            if progress is not None:
                progress(done, total, f"{site.title} / {capture.dir_name}")
            report.captures += 1
            _check_capture(settings, site, capture, report, deep=deep)
        _check_index(settings, site, report)

    report.finished_at = utcnow()
    return report


def _captures_for(session: Session, site_ids: list[int]) -> dict[int, list[Capture]]:
    if not site_ids:
        return {}
    rows = session.scalars(
        select(Capture)
        .where(Capture.site_id.in_(site_ids), Capture.status.in_(("ok", "partial")))
        .order_by(Capture.site_id, Capture.started_at)
    ).all()
    out: dict[int, list[Capture]] = {}
    for row in rows:
        out.setdefault(row.site_id, []).append(row)
    return out


def _check_capture(
    settings: Settings, site: Site, capture: Capture, report: Report, *, deep: bool
) -> None:
    try:
        capture_dir = storage.site_dir(settings, site.archive_path) / storage.CAPTURES_DIR
        root = storage.resolve_within(capture_dir, capture.dir_name)
    except storage.StoragePathError as exc:  # pragma: no cover — dir_name is ours
        report.findings.append(
            Finding("missing", site.id, site.title, capture.id, capture.dir_name, str(exc))
        )
        return

    if not root.is_dir():
        report.findings.append(
            Finding(
                "missing",
                site.id,
                site.title,
                capture.id,
                capture.dir_name,
                "the capture directory is not on disk. Restore it from a backup, or delete "
                "the capture so the index stops expecting it.",
            )
        )
        return

    artifacts = _artifacts(root, capture)
    if artifacts is None:
        report.findings.append(
            Finding(
                "no-checksums",
                site.id,
                site.title,
                capture.id,
                capture.dir_name,
                "this capture recorded no checksums, so there is nothing to compare its "
                "files against. Captures made before checksums were recorded read like this.",
            )
        )
        return

    for artifact in artifacts:
        name = str(artifact.get("name") or "")
        expected = str(artifact.get("sha256") or "")
        if not name or not expected:
            continue
        try:
            path = storage.resolve_within(root, name)
        except storage.StoragePathError:
            report.findings.append(
                Finding(
                    "missing",
                    site.id,
                    site.title,
                    capture.id,
                    capture.dir_name,
                    "the manifest names a file outside the capture directory.",
                    name,
                )
            )
            continue
        if not path.is_file():
            report.findings.append(
                Finding(
                    "missing",
                    site.id,
                    site.title,
                    capture.id,
                    capture.dir_name,
                    "the manifest lists this file and it is not on disk.",
                    name,
                )
            )
            continue

        report.files += 1
        size = path.stat().st_size
        report.bytes_read += size
        actual = postprocess.sha256_file(path)
        if actual != expected:
            report.findings.append(
                Finding(
                    "mismatch",
                    site.id,
                    site.title,
                    capture.id,
                    capture.dir_name,
                    f"the bytes on disk no longer match the checksum taken when this capture "
                    f"finished (expected {expected[:12]}…, found {actual[:12]}…). A WARC "
                    "cannot be repaired — restore this file from a backup, or capture the "
                    "site again.",
                    name,
                )
            )
            continue
        if deep and path.name.endswith((".warc", ".warc.gz")):
            problem = _readable(path)
            if problem:
                report.findings.append(
                    Finding(
                        "unreadable",
                        site.id,
                        site.title,
                        capture.id,
                        capture.dir_name,
                        problem,
                        name,
                    )
                )


def _artifacts(root: Path, capture: Capture) -> list[dict[str, Any]] | None:
    """What this capture says it wrote — from the manifest, then the database.

    The manifest first, deliberately: it travels with the archive, so a
    verification run after a database rebuild checks against what was recorded
    at the time rather than against rows reconstructed from the same disk it
    is verifying.
    """
    manifest = root / storage.MANIFEST_FILE
    if manifest.is_file():
        try:
            payload = storage.read_json(manifest)
            files = payload.get("warc_files")
            if isinstance(files, list) and files:
                return [f for f in files if isinstance(f, dict)]
        except (OSError, ValueError):
            pass
    recorded = capture.warc_files
    if isinstance(recorded, list) and recorded:
        return [f for f in recorded if isinstance(f, dict)]
    return None


def _readable(path: Path) -> str | None:
    """Parse a WARC end to end, and insist its compressed stream ends properly.

    Both halves are needed, which was measured rather than assumed. **warcio
    stops silently at a truncated tail**: a four-record file missing its last
    forty bytes parsed all four records and reported nothing, and one cut in
    half parsed two and reported nothing. Truncation is the likeliest real
    damage — a container stopped mid-write — and it is the one thing the
    checksum cannot cover either, because the checksum was taken over the file
    as it ended up.

    What warcio does catch is a mangled record inside the file, where the gzip
    layer only says "not a gzipped file". Neither check subsumes the other.
    """
    try:
        from warcio.archiveiterator import ArchiveIterator
    except ImportError:  # pragma: no cover — declared in pyproject
        return None
    try:
        with open(path, "rb") as fh:
            for record in ArchiveIterator(fh):
                record.content_stream().read()
    except Exception as exc:
        return f"the file's checksum is right but it stops parsing partway through: {exc}"

    if not path.name.endswith(".gz"):
        return None
    try:
        import gzip

        with gzip.open(path, "rb") as gz:
            while gz.read(READ_CHUNK):
                pass
    except Exception as exc:
        return (
            "the file's checksum is right and its compressed stream does not end properly, "
            f"so it was already truncated when that checksum was taken: {exc}"
        )
    return None


def _check_index(settings: Settings, site: Site, report: Report) -> None:
    """Every WARC the replay index names must exist.

    A stale index is not corruption and does not threaten the archive; it is
    replay serving 503 for pages that are still on disk, which looks exactly
    like data loss to whoever meets it.
    """
    from cairn.services import replay

    index = replay.index_path(settings, site.archive_path)
    if not index.is_file() or index.stat().st_size == 0:
        return
    site_root = storage.site_dir(settings, site.archive_path)
    missing: set[str] = set()
    try:
        with open(index, encoding="utf-8") as fh:
            for line in fh:
                parts = line.split(" ", 2)
                if len(parts) != 3:
                    continue
                try:
                    filename = str(json.loads(parts[2]).get("filename") or "")
                except ValueError:
                    continue
                if not filename or filename in missing:
                    continue
                if not (site_root / filename).is_file():
                    missing.add(filename)
    except OSError as exc:  # pragma: no cover — unreadable index
        report.findings.append(
            Finding("stale-index", site.id, site.title, None, "", f"the index is unreadable: {exc}")
        )
        return

    for filename in sorted(missing):
        report.findings.append(
            Finding(
                "stale-index",
                site.id,
                site.title,
                None,
                "",
                "the replay index still points at this file. Rebuild the index to clear it.",
                filename,
            )
        )


# ── the report on disk, and when to run again ────────────────────────────


def report_path(settings: Settings) -> Path:
    return settings.config_dir / REPORT_FILE


def save(settings: Settings, report: Report) -> None:
    storage.write_json(report_path(settings), report.to_dict())


def load(settings: Settings) -> dict[str, Any] | None:
    path = report_path(settings)
    if not path.is_file():
        return None
    try:
        data = storage.read_json(path)
    except (OSError, ValueError):  # pragma: no cover — hand-edited file
        return None
    return data if isinstance(data, dict) else None


def due(session: Session, settings: Settings, *, now: datetime | None = None) -> bool:
    from cairn.services import settings_store

    days = settings_store.get_int(session, INTERVAL_SETTING, 7)
    if days <= 0:
        return False
    last = load(settings)
    if not last or not last.get("finished_at"):
        return True
    try:
        finished = datetime.fromisoformat(str(last["finished_at"]).replace("Z", "+00:00"))
    except ValueError:  # pragma: no cover — hand-edited file
        return True
    return (now or utcnow()) - finished >= timedelta(days=days)


def health(session: Session, settings: Settings) -> dict[str, Any]:
    """The archive-health summary: size, coverage, and the oldest unverified.

    "Oldest unverified capture" is the number worth watching. A verification
    that has been failing to reach half the archive reports success on the
    half it does reach, and only this makes that visible.
    """
    last = load(settings)
    verified_dirs: set[str] = set()
    if last:
        # Anything that produced a finding is not verified, whatever else the
        # run touched.
        broken = {str(f.get("capture_dir") or "") for f in last.get("findings") or []}
    else:
        broken = set()

    rows = list(
        session.execute(
            select(Capture.id, Capture.dir_name, Capture.started_at, Capture.site_id, Site.title)
            .join(Site, Site.id == Capture.site_id)
            .where(Site.deleted_at.is_(None), Capture.status.in_(("ok", "partial")))
            .order_by(Capture.started_at)
        ).all()
    )
    finished = last.get("finished_at") if last else None
    for row in rows:
        if finished and row.dir_name not in broken:
            verified_dirs.add(row.dir_name)

    oldest = None
    for row in rows:
        if row.dir_name not in verified_dirs:
            oldest = {
                "capture_id": row.id,
                "site_id": row.site_id,
                "site_title": row.title,
                "dir_name": row.dir_name,
                "started_at": to_iso(row.started_at),
            }
            break

    return {
        "captures": len(rows),
        "verified": len(verified_dirs),
        "oldest_unverified": oldest,
        "last_run": last,
        "due": due(session, settings),
    }
