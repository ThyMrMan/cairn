"""Knowing whether a copy of the archive is any good.

docs/13 asks for "sync archives to a second instance — real 3-2-1". This is
not that, and the difference is the point.

**Making the copy is a solved problem and not ours.** WARCs are immutable
(D2), so a copy is append-only, which is precisely the case `rsync`, `restic`
and `rclone` are built for — with resumption, bandwidth limits, encryption and
deduplication that a bespoke sync would spend years catching up to. docs/14
already names restic as ideal for this tree. Writing a second, worse rsync
that also needed a network protocol between two instances and an auth scheme
between them would be a large amount of work to arrive somewhere behind where
`rsync -a` already is.

**Knowing the copy is good is not solved, and is ours.** rsync reports that it
transferred bytes. It cannot tell you that every capture this instance knows
about is present in the copy, that each file still hashes to what was recorded
when it was written, or that the WARCs still parse. That is exactly the
information in `manifest.json` and the integrity verifier, and this instance is
the only thing that has it.

So: make the copy with whatever tool you like, mount it, and point this at it.
The failure it exists to catch is the one docs/13 raises about integrity — you
find out during a restore — moved from the archive to the backup of it, where
it is worse, because the backup is the thing you were counting on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, Site
from cairn.logging import get_logger
from cairn.services import storage

log = get_logger(__name__)

MAX_LISTED = 50


class MirrorError(ValueError):
    """The mirror path cannot be used."""


@dataclass(slots=True)
class SiteCoverage:
    site_id: int
    title: str
    archive_path: str
    captures: int = 0
    present: int = 0
    missing: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.present == self.captures

    def to_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "title": self.title,
            "archive_path": self.archive_path,
            "captures": self.captures,
            "present": self.present,
            "missing": self.missing[:20],
            "complete": self.complete,
        }


@dataclass(slots=True)
class Survey:
    root: str
    sites: list[SiteCoverage] = field(default_factory=list)
    captures: int = 0
    present: int = 0
    unknown_dirs: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.captures == self.present

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "captures": self.captures,
            "present": self.present,
            "missing": self.captures - self.present,
            "complete": self.complete,
            "sites": [s.to_dict() for s in self.sites if not s.complete][:MAX_LISTED],
            "complete_sites": sum(1 for s in self.sites if s.complete),
            "unknown_dirs": self.unknown_dirs[:20],
        }


def require_root(settings: Settings, path: str) -> Path:
    """The mirror path, checked for the two mistakes that make a nonsense of it."""
    candidate = Path((path or "").strip())
    if not candidate.is_absolute():
        raise MirrorError("Give an absolute path to the copy, as this container sees it.")
    if not candidate.is_dir():
        raise MirrorError(
            f"{candidate} is not a directory this container can see. Mount the backup into "
            "the container and use the path inside it."
        )
    here = settings.data_dir.resolve()
    there = candidate.resolve()
    if there == here or there.is_relative_to(here) or here.is_relative_to(there):
        raise MirrorError(
            f"{candidate} is inside this instance's own data directory, so checking it would "
            "check the archive against itself. Point this at a mounted copy."
        )
    return there


def survey(session: Session, settings: Settings, root: Path) -> Survey:
    """Which captures the copy has, and which it does not.

    A directory listing rather than a checksum pass: this is the cheap
    question — is the backup *complete* — and it is the one that is usually
    answered wrong, because a sync that skipped a directory reports success.
    """
    result = Survey(root=str(root))
    sites = list(session.scalars(select(Site).where(Site.deleted_at.is_(None))).all())
    known_paths = {site.archive_path for site in sites}

    for site in sites:
        coverage = SiteCoverage(site_id=site.id, title=site.title, archive_path=site.archive_path)
        captures = session.scalars(
            select(Capture)
            .where(Capture.site_id == site.id, Capture.status.in_(("ok", "partial")))
            .order_by(Capture.started_at)
        ).all()
        try:
            site_root = storage.site_dir_under(root, settings, site.archive_path)
        except storage.StoragePathError:  # pragma: no cover — archive_path is ours
            coverage.missing.append("(unreadable path)")
            result.sites.append(coverage)
            continue
        for capture in captures:
            coverage.captures += 1
            try:
                directory = storage.resolve_within(
                    site_root / storage.CAPTURES_DIR, capture.dir_name
                )
            except storage.StoragePathError:  # pragma: no cover — dir_name is ours
                coverage.missing.append(capture.dir_name)
                continue
            if directory.is_dir() and any(directory.rglob("*.warc.gz")):
                coverage.present += 1
            else:
                coverage.missing.append(capture.dir_name)
        result.captures += coverage.captures
        result.present += coverage.present
        result.sites.append(coverage)

    result.unknown_dirs = _unknown(
        root / settings.archives_dir.relative_to(settings.data_dir), known_paths
    )
    return result


def _unknown(root: Path, known: set[str]) -> list[str]:
    """Site directories in the copy that this instance has never heard of.

    Not an error — an old backup holds sites since deleted, which is often the
    reason for having one — but worth saying, because the other explanation is
    that the copy and the database belong to different instances.
    """
    found: list[str] = []
    if not root.is_dir():
        return found
    for candidate in sorted(root.rglob("site.yaml")):
        relative = candidate.parent.relative_to(root).as_posix()
        if relative not in known:
            found.append(relative)
        if len(found) >= 50:
            break
    return found
