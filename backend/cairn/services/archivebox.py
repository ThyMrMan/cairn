"""Importing an existing ArchiveBox archive.

The premise from docs/13: you already have an ArchiveBox instance with work in
it, and it is already producing per-page WARCs that only need indexing —
which is what this tool does. So the import is a copy and an index, not a
conversion.

**The schema was read from a real ArchiveBox, not from memory.** docs/13 warns
that the `index.sqlite3` layout has shifted across versions, so a 0.7.4 was
run against a fixture site and the tables it wrote were read back:

    core_snapshot        id, timestamp, title, added, updated, url
    core_archiveresult   snapshot_id, extractor, status, output, cmd, pwd, …
    core_tag             id, name, slug
    core_snapshot_tags   snapshot_id, tag_id

Anything without `core_snapshot` is refused by name rather than half-imported.

**A domain becomes a site and the whole import becomes one capture.** The
alternative — one capture per snapshot — gives a domain with five hundred
archived pages five hundred captures of one page each, which is unreadable in
the UI and wrong about what a capture is. Nothing is lost by grouping: the
CDXJ records each response's own date, and replay's time dimension comes from
the index rather than from the directory a WARC sits in.

**The index is opened read-only.** It is somebody's live ArchiveBox database
and this has no business writing to it, including by accident.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, Site
from cairn.db.types import utcnow
from cairn.logging import get_logger
from cairn.services import storage
from cairn.services.sites import SiteError

log = get_logger(__name__)

INDEX_FILE = "index.sqlite3"
ARCHIVE_DIR = "archive"
ENGINE_ID = "archivebox-import"
CAPTURE_KIND = "import"

# Read from a real ArchiveBox 0.7.4. An archive without this table is either
# much older than anything this can read or not an ArchiveBox archive at all.
REQUIRED_TABLE = "core_snapshot"
REQUIRED_COLUMNS = frozenset({"id", "timestamp", "url"})


class ArchiveBoxError(RuntimeError):
    """The archive could not be read or imported."""


@dataclass(slots=True)
class Snapshot:
    id: str
    timestamp: str
    url: str
    title: str
    added: datetime | None
    warcs: list[Path] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()


@dataclass(slots=True)
class Survey:
    """What an archive contains, before anything is copied."""

    version: str = ""
    snapshots: int = 0
    with_warcs: int = 0
    without_warcs: int = 0
    warc_bytes: int = 0
    hosts: dict[str, int] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "snapshots": self.snapshots,
            "with_warcs": self.with_warcs,
            "without_warcs": self.without_warcs,
            "warc_bytes": self.warc_bytes,
            "hosts": self.hosts,
            "tags": self.tags,
            "problems": self.problems,
        }


@dataclass(slots=True)
class ImportResult:
    sites: list[str] = field(default_factory=list)
    snapshots: int = 0
    warcs: int = 0
    bytes: int = 0
    skipped: int = 0
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sites": self.sites,
            "snapshots": self.snapshots,
            "warcs": self.warcs,
            "bytes": self.bytes,
            "skipped": self.skipped,
            "problems": self.problems,
        }


# ── reading ──────────────────────────────────────────────────────────────


def _connect(root: Path) -> sqlite3.Connection:
    index = root / INDEX_FILE
    if not index.is_file():
        raise ArchiveBoxError(
            f"no {INDEX_FILE} in {root}. Point this at an ArchiveBox data directory — the one "
            "holding index.sqlite3 and archive/ — and make sure it is mounted into this "
            "container."
        )
    # Read-only, and immutable=0 so a live ArchiveBox writing to it is still
    # readable. This must never write to somebody's own archive.
    connection = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _check_schema(connection: sqlite3.Connection) -> None:
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if REQUIRED_TABLE not in tables:
        raise ArchiveBoxError(
            f"that index has no {REQUIRED_TABLE} table, so it is not an ArchiveBox archive this "
            "can read. Versions before 0.6 kept a different layout and are not supported; "
            "upgrading ArchiveBox in place migrates the index."
        )
    columns = {r[1] for r in connection.execute(f"PRAGMA table_info({REQUIRED_TABLE})")}
    missing = REQUIRED_COLUMNS - columns
    if missing:
        raise ArchiveBoxError(
            f"{REQUIRED_TABLE} is missing {', '.join(sorted(missing))}, so this ArchiveBox is a "
            "version with a layout this cannot read."
        )


def read_snapshots(root: Path) -> list[Snapshot]:
    """Every snapshot with the WARCs it actually has on disk."""
    connection = _connect(root)
    try:
        _check_schema(connection)
        tags = _tags_by_snapshot(connection)
        rows = connection.execute(
            "SELECT id, timestamp, url, title, added FROM core_snapshot ORDER BY added, timestamp"
        ).fetchall()
    finally:
        connection.close()

    snapshots: list[Snapshot] = []
    for row in rows:
        timestamp = str(row["timestamp"] or "")
        if not timestamp or not str(row["url"] or ""):
            continue
        try:
            directory = storage.resolve_within(root / ARCHIVE_DIR, timestamp)
        except storage.StoragePathError:
            continue
        snapshot = Snapshot(
            id=str(row["id"]),
            timestamp=timestamp,
            url=str(row["url"]),
            title=str(row["title"] or ""),
            added=_stamp(row["added"]),
            tags=tags.get(str(row["id"]), []),
        )
        warc_dir = directory / "warc"
        if warc_dir.is_dir():
            # Globbed rather than constructed: ArchiveBox names the WARC after
            # the moment wget ran, which is not the snapshot's own timestamp —
            # measured, a snapshot at 1786483154.389263 held 1786483155.warc.gz.
            snapshot.warcs = sorted(warc_dir.glob("*.warc.gz")) + sorted(warc_dir.glob("*.warc"))
        snapshots.append(snapshot)
    return snapshots


def _tags_by_snapshot(connection: sqlite3.Connection) -> dict[str, list[str]]:
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"core_tag", "core_snapshot_tags"} <= tables:
        return {}
    out: dict[str, list[str]] = defaultdict(list)
    for row in connection.execute(
        "SELECT st.snapshot_id AS sid, t.name AS name "
        "FROM core_snapshot_tags st JOIN core_tag t ON t.id = st.tag_id"
    ):
        name = str(row["name"] or "").strip()
        if name:
            out[str(row["sid"])].append(name)
    return dict(out)


def _stamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def survey(root: Path) -> Survey:
    """What is in there, without touching anything.

    The dry run. An import of somebody's whole ArchiveBox is minutes and
    gigabytes, and the first question is always how much of it has a WARC —
    ArchiveBox archives plenty of pages with extractors that produce no WARC
    at all, and those cannot be imported into a WARC archive.
    """
    connection = _connect(root)
    try:
        _check_schema(connection)
        result = Survey(version=_layout(connection))
    finally:
        connection.close()
    snapshots = read_snapshots(root)
    result.snapshots = len(snapshots)

    hosts: dict[str, int] = defaultdict(int)
    tags: set[str] = set()
    for snapshot in snapshots:
        tags.update(snapshot.tags)
        if not snapshot.warcs:
            result.without_warcs += 1
            continue
        result.with_warcs += 1
        hosts[snapshot.host or "(no host)"] += 1
        result.warc_bytes += sum(w.stat().st_size for w in snapshot.warcs if w.is_file())

    result.hosts = dict(sorted(hosts.items(), key=lambda kv: (-kv[1], kv[0])))
    result.tags = sorted(tags)
    if result.without_warcs:
        result.problems.append(
            f"{result.without_warcs} snapshot(s) have no WARC. ArchiveBox only writes one when "
            "its wget extractor ran and succeeded, so those pages cannot come across."
        )
    if not result.with_warcs:
        result.problems.append("Nothing here has a WARC, so there is nothing to import.")
    return result


def _layout(connection: sqlite3.Connection) -> str:
    """Which layout this index is in, from the tables themselves.

    Not from `ArchiveBox.conf`: a real 0.7.4 writes its Django `SECRET_KEY`
    into that file and no version at all, so reading it would mean handling
    somebody's secret to learn nothing.
    """
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {"core_snapshot", "core_archiveresult"} <= tables:
        return "0.6+"
    return "unknown"


# ── importing ────────────────────────────────────────────────────────────


def import_archive(
    session: Session,
    settings: Settings,
    root: Path,
    *,
    hosts: list[str] | None = None,
    folder_id: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> ImportResult:
    """Copy each domain's WARCs into a site of its own and index them.

    Copied rather than moved: the ArchiveBox archive stays exactly as it was,
    so this is repeatable and reversible by deleting the sites it made.
    """

    result = ImportResult()
    snapshots = [s for s in read_snapshots(root) if s.warcs]
    if hosts:
        wanted = {h.lower() for h in hosts}
        snapshots = [s for s in snapshots if s.host in wanted]
    if not snapshots:
        raise ArchiveBoxError("nothing in that archive has a WARC to import")

    by_host: dict[str, list[Snapshot]] = defaultdict(list)
    for snapshot in snapshots:
        by_host[snapshot.host or "unknown"].append(snapshot)

    now = utcnow()
    for host, group in sorted(by_host.items()):
        if progress is not None:
            progress(f"{host}: {len(group)} snapshot(s)")
        try:
            _import_host(
                session, settings, root, host, group, now, folder_id=folder_id, result=result
            )
        except (SiteError, OSError, storage.StoragePathError) as exc:
            # An archive accumulated over years has entries nobody remembers
            # adding, and one of them being unusable is not a reason to abandon
            # the other four hundred.
            result.skipped += len(group)
            result.problems.append(f"{host}: skipped ({exc})")
            session.rollback()
    return result


def _import_host(
    session: Session,
    settings: Settings,
    root: Path,
    host: str,
    group: list[Snapshot],
    now: datetime,
    *,
    folder_id: int | None,
    result: ImportResult,
) -> None:
    from cairn.services import replay
    from cairn.services import sites as site_service
    from cairn.services import tags as tag_service

    site = site_service.create_site(
        session,
        settings,
        title=host,
        seed_url=_seed_of(group),
        folder_id=folder_id,
    )
    capture = Capture(
        site_id=site.id,
        kind=CAPTURE_KIND,
        engine_id=ENGINE_ID,
        dir_name=storage.capture_dir_name(now, CAPTURE_KIND, ENGINE_ID),
        status="ok",
        started_at=now,
        finished_at=utcnow(),
    )
    session.add(capture)
    session.flush()

    copied, size = _copy_warcs(settings, site, capture, group)
    result.warcs += len(copied)
    result.bytes += size
    result.snapshots += len(group)
    result.sites.append(site.slug)

    capture.warc_files = copied
    capture.bytes_written = size
    session.flush()

    names = sorted({name for snapshot in group for name in snapshot.tags})
    if names:
        tag_service.add_to_sites(session, [site.id], names)

    try:
        index = replay.build_index(settings, site.archive_path)
        capture.indexed_at = utcnow()
        if not index.records:
            result.problems.append(f"{host}: the WARCs indexed to no records")
    except replay.ReplayError as exc:
        result.problems.append(f"{host}: could not be indexed ({exc})")
    session.flush()


def _seed_of(group: list[Snapshot]) -> str:
    """The shortest URL in the group, which is usually the site's root."""
    return min((s.url for s in group), key=lambda u: (len(urlsplit(u).path or "/"), u))


def _copy_warcs(
    settings: Settings, site: Site, capture: Capture, group: list[Snapshot]
) -> tuple[list[dict[str, Any]], int]:
    from cairn.services.postprocess import sha256_file

    capture_dir = storage.ensure_capture_dirs(settings, site.archive_path, capture.dir_name)
    warc_dir = capture_dir / storage.WARC_DIR
    artifacts: list[dict[str, Any]] = []
    total = 0
    taken: set[str] = set()

    for snapshot in group:
        for source in snapshot.warcs:
            # Prefixed with the snapshot's own timestamp: two snapshots can
            # hold WARCs with the same basename, and a capture directory that
            # loses one of them loses those pages silently.
            name = f"{snapshot.timestamp}-{source.name}"
            candidate, n = name, 2
            while candidate in taken:
                candidate = f"{snapshot.timestamp}-{n}-{source.name}"
                n += 1
            taken.add(candidate)

            target = warc_dir / candidate
            shutil.copy2(source, target)
            size = target.stat().st_size
            total += size
            artifacts.append(
                {
                    "name": f"{storage.WARC_DIR}/{candidate}",
                    "kind": "warc",
                    "size": size,
                    "sha256": sha256_file(target),
                }
            )
    return artifacts, total
