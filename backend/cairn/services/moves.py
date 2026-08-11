"""Moving archives between folders.

The database rows and the directories have to end up agreeing, and the move
itself is the one step that can fail slowly. So the order is always: move the
directory first, then write the rows, and put the directory back if the rows
will not take. A rename that succeeded while the database still points at the
old path is a site that has vanished from the UI and from replay; the reverse
— rows updated, files not moved — is worse, because the next capture writes
into a directory that is not the archive.

Two ways a move can go, decided by the filesystem rather than by us:

  - **A rename.** Instant, whatever the directory holds, because only the
    entry moves. This is every move inside one filesystem, which is every
    normal install.
  - **A copy.** Only when the ends are on different filesystems, which on
    Unraid means `/data` spanning array disks behind FUSE. Minutes, and a
    second copy of the bytes while it runs. That one goes through a job, so it
    gets a progress row, cancellation, and crash recovery like a capture.

`storage.rename_directory` refuses to make that decision silently: it raises
`CrossDeviceMoveError` rather than quietly falling back, so the difference between
an instant operation and a ten-minute one cannot hide inside a request.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Folder, Job, Site
from cairn.db.types import utcnow
from cairn.logging import get_logger
from cairn.services import folders, replay, storage, symlinks
from cairn.services import sites as site_service
from cairn.services.storage import CrossDeviceMoveError

log = get_logger(__name__)


class MoveError(RuntimeError):
    """A move could not be carried out."""


class SiteBusyError(MoveError):
    """Something is running against this site, so its files must not move."""


@dataclass(frozen=True, slots=True)
class MoveResult:
    site_ids: list[int]
    old_path: str
    new_path: str
    method: str  # rename | copy | noop


# ── guards ───────────────────────────────────────────────────────────────


def assert_idle(session: Session, site_ids: list[int]) -> None:
    """Refuse to move anything a job is working on.

    A capture writes into `<archive_path>/captures/…` by an absolute path
    resolved when the job started. Moving the directory underneath it does not
    fail — wget keeps writing happily into the old inode, and the WARC lands
    somewhere the database has no name for.
    """
    if not site_ids:
        return
    busy = session.scalar(
        select(Job.site_id)
        .where(Job.site_id.in_(site_ids), Job.status.in_(("queued", "running")))
        .limit(1)
    )
    if busy is not None:
        raise SiteBusyError(
            "A job is queued or running for one of these sites. Wait for it to "
            "finish or cancel it, then move them."
        )


# ── sites ────────────────────────────────────────────────────────────────


def move_site(
    session: Session,
    settings: Settings,
    site: Site,
    target: Folder,
    *,
    copy_ok: bool = False,
) -> MoveResult:
    """Move one site's directory into another folder.

    The slug can change here, and only here. It is otherwise stable identity —
    renaming a site's *title* must never move its files (docs/03) — but
    `UNIQUE(folder_id, slug)` has to hold, so a site called `example` moving
    into a folder that already has an `example` becomes `example-2`.
    """
    assert_idle(session, [site.id])
    if site.folder_id == target.id:
        return MoveResult([site.id], site.archive_path, site.archive_path, "noop")

    taken = set(
        session.scalars(
            select(Site.slug).where(Site.folder_id == target.id, Site.id != site.id)
        ).all()
    )
    slug = storage.unique_slug(site.slug, taken)
    new_path = f"{target.path}/{slug}" if target.path else slug

    old_path = site.archive_path
    method = "noop"
    if site.deleted_at is None:
        method = _move_directory(
            storage.site_dir(settings, old_path),
            storage.resolve_within(settings.archives_dir, new_path),
            copy_ok=copy_ok,
        )

    try:
        site.folder_id = target.id
        site.slug = slug
        site.archive_path = new_path
        site.updated_at = utcnow()
        session.flush()
    except Exception:
        if method != "noop":
            _undo(settings, new_path, old_path)
        raise

    _settle(session, settings, [site])
    log.info("site moved", extra={"site": site.id, "from": old_path, "to": new_path})
    return MoveResult([site.id], old_path, new_path, method)


# ── folders ──────────────────────────────────────────────────────────────


def relocate(
    session: Session,
    settings: Settings,
    plan: folders.Relocation,
    *,
    copy_ok: bool = False,
) -> MoveResult:
    """Carry out a folder rename or reparent.

    One directory moves. Everything underneath — descendant folders, every
    site's directory, every capture inside those — comes with it for free,
    which is why a folder holding forty sites renames as fast as an empty one.
    The expensive half is the database, and even that is a few hundred rows.
    """
    site_ids = [site.id for site, _ in plan.sites]
    assert_idle(session, site_ids)

    if plan.is_noop:
        return MoveResult(site_ids, plan.old_path, plan.new_path, "noop")

    method = "noop"
    if plan.source.exists():
        method = _move_directory(plan.source, plan.target, copy_ok=copy_ok)
    else:
        # No directory yet — a folder created before anything was put in it.
        plan.target.mkdir(parents=True, exist_ok=True)

    try:
        folders.apply(plan)
        session.flush()
    except Exception:
        if method != "noop":
            _undo(settings, plan.new_path, plan.old_path)
        raise

    _settle(session, settings, [site for site, _ in plan.sites])
    log.info(
        "folder moved", extra={"from": plan.old_path, "to": plan.new_path, "sites": len(site_ids)}
    )
    return MoveResult(site_ids, plan.old_path, plan.new_path, method)


def delete_folder(
    session: Session, settings: Settings, folder: Folder, *, reassign_to: int | None = None
) -> int:
    """Delete a folder after its sites have somewhere else to be."""
    sites, target = folders.check_deletable(session, folder, reassign_to=reassign_to)

    moved = 0
    if target is not None:
        for site in sites:
            move_site(session, settings, site, target)
            moved += 1

    directory = storage.resolve_within(settings.archives_dir, folder.path)
    session.delete(folder)
    session.flush()

    if directory.is_dir():
        try:
            directory.rmdir()
        except OSError as exc:
            # Not empty means something is in there we did not put there. The
            # folder is gone from the UI either way; leaving the directory is
            # the only option that cannot destroy data.
            log.warning(
                "folder row deleted but its directory was not empty",
                extra={"path": str(directory), "err": str(exc)},
            )
    return moved


# ── plumbing ─────────────────────────────────────────────────────────────


def _move_directory(source, target, *, copy_ok: bool) -> str:  # type: ignore[no-untyped-def]
    try:
        storage.rename_directory(source, target)
        return "rename"
    except CrossDeviceMoveError:
        if not copy_ok:
            raise
        storage.copy_directory_into_place(source, target)
        return "copy"
    except storage.StoragePathError as exc:
        raise MoveError(str(exc)) from exc


def _undo(settings: Settings, current: str, original: str) -> None:
    """Put a directory back after the database refused the change."""
    try:
        storage.rename_directory(
            storage.resolve_within(settings.archives_dir, current),
            storage.resolve_within(settings.archives_dir, original),
        )
    except OSError as exc:  # pragma: no cover — the filesystem just took it
        log.error(
            "could not undo a directory move; the archive is at the new path but the "
            "database still names the old one",
            extra={"at": current, "expected": original, "err": str(exc)},
        )


def _settle(session: Session, settings: Settings, moved: list[Site]) -> None:
    """Re-point everything that names a site by path.

    The replay collection is a pair of symlinks into the site directory, so it
    is dangling the moment the directory moves — verified against pywb 2.9.1:
    a stale link answers 404, and a re-pointed one answers correctly on the
    very next request, with no restart. So this is all a move owes replay.
    """
    for site in moved:
        if site.deleted_at is not None:
            continue
        try:
            replay.link_collection(settings, site.id, site.archive_path)
        except replay.ReplayError as exc:
            log.warning(
                "could not re-link replay after a move", extra={"site": site.id, "err": str(exc)}
            )
        site_service.write_site_yaml(session, settings, site)
    symlinks.safe_rebuild(session, settings)
