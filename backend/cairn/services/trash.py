"""Deleting a site, undoing that, and eventually meaning it.

Deleting an archive is the one destructive thing this tool does, and the thing
being destroyed took hours to fetch and cannot be re-fetched once the source
is gone — which is the entire reason the source was worth archiving. So delete
moves the directory to `/data/trash` and marks the row, and only a purge
actually unlinks bytes.

The retention sweep runs at boot rather than on a timer, because there is no
scheduler until M6. That is a real difference and worth stating: a container
left running for two months purges nothing, and the number of days is a floor
on how long something is kept, never a promise about when it goes. A boot
sweep is enough for the thing retention is actually for — not letting deleted
archives quietly hold the array forever — and `POST /api/maintenance/purge-
trash` covers the case where somebody wants the space back now.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Folder, Job, Site
from cairn.db.types import utcnow
from cairn.logging import get_logger
from cairn.services import replay, search, settings_store, storage, symlinks
from cairn.services import sites as site_service

log = get_logger(__name__)

RETENTION_SETTING = "storage.trash_retention_days"
DEFAULT_RETENTION_DAYS = 30


class TrashError(RuntimeError):
    """A site could not be trashed, restored or purged."""


@dataclass(frozen=True, slots=True)
class TrashedSite:
    site: Site
    path: Path
    exists: bool
    size_bytes: int
    purge_after_days: int | None


def trash_path(settings: Settings, site: Site) -> Path:
    """Where a trashed site's directory lives.

    Keyed by id as well as slug: two sites in different folders can share a
    slug, and trash is flat, so the id is what stops one deletion from
    landing on top of another.
    """
    return settings.trash_dir / f"{site.id}-{site.slug}"


def retention_days(session: Session) -> int:
    return settings_store.get_int(session, RETENTION_SETTING, DEFAULT_RETENTION_DAYS)


# ── delete ───────────────────────────────────────────────────────────────


def trash_site(session: Session, settings: Settings, site: Site) -> None:
    """Mark deleted and move the directory aside, in that order.

    The row first, so a failure moving files leaves a hidden site rather than
    a visible site with no archive behind it.
    """
    _assert_idle(session, site)

    site.deleted_at = utcnow()
    site.status = "archived"
    session.flush()

    # Unlink replay before the files move. A collection left pointing at a
    # directory that is no longer there is not harmless: pywb keeps serving the
    # collection name, answering 404 for every URL, which reads as "the archive
    # is broken" rather than "the site was deleted".
    replay.unlink_collection(settings, site.id)
    symlinks.safe_rebuild(session, settings)

    source = storage.site_dir(settings, site.archive_path)
    if not source.exists():
        return
    target = trash_path(settings, site)
    try:
        settings.trash_dir.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        storage.rename_directory(source, target)
    except (OSError, storage.StoragePathError) as exc:
        log.error("could not move site to trash", extra={"site": site.id, "err": str(exc)})


# ── restore ──────────────────────────────────────────────────────────────


def restore_site(session: Session, settings: Settings, site: Site) -> Site:
    """Put a trashed site back where it came from.

    Its folder may have been deleted and its slug may have been taken by
    something created since, so neither is assumed: the folder falls back to
    the default one and the slug gets a suffix, rather than the restore
    failing on a collision the user cannot see.
    """
    from cairn.services import folders

    if site.deleted_at is None:
        raise TrashError("that site is not in the trash")

    folder = session.get(Folder, site.folder_id)
    if folder is None:  # pragma: no cover — RESTRICT normally prevents this
        folder = folders.root_folder(session)
        site.folder_id = folder.id

    taken = set(
        session.scalars(
            select(Site.slug).where(Site.folder_id == folder.id, Site.id != site.id)
        ).all()
    )
    slug = storage.unique_slug(site.slug, taken)
    archive_path = f"{folder.path}/{slug}" if folder.path else slug

    source = trash_path(settings, site)
    if source.exists():
        target = storage.resolve_within(settings.archives_dir, archive_path)
        if target.exists():
            raise TrashError(f"{archive_path} already exists; nothing was restored")
        try:
            storage.rename_directory(source, target)
        except storage.CrossDeviceMoveError:
            storage.copy_directory_into_place(source, target)

    site.slug = slug
    site.archive_path = archive_path
    site.deleted_at = None
    # Whether it is ready depends on whether it has ever been captured, which
    # is exactly what `last_capture_at` says.
    site.status = "ready" if site.last_capture_at else "new"
    site.updated_at = utcnow()
    session.flush()

    try:
        replay.link_collection(settings, site.id, site.archive_path)
    except replay.ReplayError as exc:
        log.warning(
            "restored, but replay could not be re-linked", extra={"site": site.id, "err": str(exc)}
        )
    site_service.write_site_yaml(session, settings, site)
    symlinks.safe_rebuild(session, settings)
    log.info("site restored", extra={"site": site.id, "path": archive_path})
    return site


# ── purge ────────────────────────────────────────────────────────────────


def purge_site(session: Session, settings: Settings, site: Site) -> int:
    """Delete the archive and the row. Nothing about this is recoverable."""
    if site.deleted_at is None:
        raise TrashError("only a site already in the trash can be purged")

    directory = trash_path(settings, site)
    freed = storage.directory_size(directory) if directory.exists() else 0
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)
    # Belt and braces: if the delete failed to move the directory, it is still
    # in the archive tree and purging must take it too.
    stray = storage.site_dir(settings, site.archive_path)
    if stray.exists():
        freed += storage.directory_size(stray)
        shutil.rmtree(stray, ignore_errors=True)

    replay.unlink_collection(settings, site.id)
    site_id = site.id
    # The FTS index has no foreign key, so nothing cascades into it.
    search.drop_site(session, site.id)
    session.delete(site)
    session.flush()
    symlinks.safe_rebuild(session, settings)
    log.info("site purged", extra={"site": site_id, "freed": freed})
    return freed


def purge_expired(session: Session, settings: Settings) -> tuple[int, int]:
    """Purge everything past the retention window. Returns (sites, bytes).

    A retention of 0 means "purge on the next sweep", not "purge instantly at
    the moment of deletion" — the trash still gets the directory, so a delete
    made by mistake is recoverable until the next boot.
    """
    days = retention_days(session)
    cutoff = utcnow() - timedelta(days=max(days, 0))
    stale = session.scalars(
        select(Site).where(Site.deleted_at.isnot(None), Site.deleted_at <= cutoff)
    ).all()

    purged = 0
    freed = 0
    for site in stale:
        try:
            freed += purge_site(session, settings, site)
            purged += 1
        except (OSError, TrashError) as exc:  # pragma: no cover — permissions
            log.warning("could not purge a trashed site", extra={"site": site.id, "err": str(exc)})
    if purged:
        log.info("purged expired trash", extra={"sites": purged, "bytes": freed, "days": days})
    return purged, freed


# ── reads ────────────────────────────────────────────────────────────────


def list_trash(session: Session, settings: Settings) -> list[TrashedSite]:
    rows = session.scalars(
        select(Site).where(Site.deleted_at.isnot(None)).order_by(Site.deleted_at.desc())
    ).all()
    days = retention_days(session)
    now = utcnow()

    out: list[TrashedSite] = []
    for site in rows:
        path = trash_path(settings, site)
        exists = path.exists()
        elapsed = (now - site.deleted_at).days if site.deleted_at else 0
        out.append(
            TrashedSite(
                site=site,
                path=path,
                exists=exists,
                size_bytes=storage.directory_size(path) if exists else 0,
                purge_after_days=max(days - elapsed, 0),
            )
        )
    return out


def trash_size(settings: Settings) -> int:
    return storage.directory_size(settings.trash_dir)


def _assert_idle(session: Session, site: Site) -> None:
    running = session.scalar(
        select(Job.id).where(Job.site_id == site.id, Job.status.in_(("queued", "running"))).limit(1)
    )
    if running is not None:
        raise TrashError("A job is still running for this site. Cancel it first.")
