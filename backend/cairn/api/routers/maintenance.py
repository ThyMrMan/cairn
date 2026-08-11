"""Trash and the repair actions (docs/09).

Everything here operates on derived or deleted data, which is why it is
separate from the routers that create things. The two rebuild endpoints are
the answer to "the tree looks wrong": both regenerate from the database, both
are safe to run at any time, and neither can lose an archive.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from cairn.api.deps import AppSettings, ClientIp, Csrf, CurrentUser, DbSession
from cairn.api.errors import ApiError
from cairn.api.schemas import Ok, TrashEntry
from cairn.db.models import Folder
from cairn.services import audit, replay, symlinks, trash

router = APIRouter(tags=["maintenance"], dependencies=[Csrf])


@router.get("/trash", response_model=list[TrashEntry])
def list_trash(db: DbSession, settings: AppSettings, _user: CurrentUser) -> list[TrashEntry]:
    entries = []
    for row in trash.list_trash(db, settings):
        folder = db.get(Folder, row.site.folder_id)
        entries.append(
            TrashEntry(
                id=row.site.id,
                slug=row.site.slug,
                title=row.site.title,
                seed_url=row.site.seed_url,
                folder_path=folder.path if folder else "",
                deleted_at=row.site.deleted_at,
                size_bytes=row.size_bytes,
                on_disk=row.exists,
                purge_after_days=row.purge_after_days,
            )
        )
    return entries


@router.post("/maintenance/purge-trash")
def purge_trash(
    db: DbSession, settings: AppSettings, user: CurrentUser, ip: ClientIp
) -> dict[str, int]:
    """Purge everything past the retention window. Not reversible."""
    purged, freed = trash.purge_expired(db, settings)
    audit.record(
        db,
        "trash.purge",
        actor=user.username,
        target=f"{purged} site(s)",
        ip=ip,
        detail={"freed_bytes": freed},
    )
    return {"purged": purged, "freed_bytes": freed}


@router.post("/maintenance/rebuild-symlinks")
def rebuild_symlinks(
    db: DbSession, settings: AppSettings, user: CurrentUser, ip: ClientIp
) -> dict[str, int]:
    try:
        linked, removed = symlinks.rebuild(db, settings)
    except OSError as exc:
        raise ApiError(
            "storage_error", f"the tag tree could not be written: {exc}", status_code=500
        ) from exc
    audit.record(db, "maintenance.symlinks", actor=user.username, ip=ip)
    return {"linked": linked, "removed": removed}


@router.post("/maintenance/rebuild-collections")
def rebuild_collections(
    db: DbSession, settings: AppSettings, user: CurrentUser, ip: ClientIp
) -> dict[str, int]:
    """Re-point every pywb collection at where its site actually is.

    The repair for a replay tab that 404s after a restore or a manual move on
    the share. A collection is two symlinks and pywb re-resolves them per
    request, so this takes effect without restarting anything.
    """
    linked, removed = replay.sync_collections(db, settings)
    audit.record(db, "maintenance.collections", actor=user.username, ip=ip)
    return {"linked": linked, "removed": removed}


@router.get("/storage")
def storage_report(db: DbSession, settings: AppSettings, _user: CurrentUser) -> dict[str, Any]:
    from cairn.services import usage

    return usage.report(db, settings).to_dict()


@router.delete("/trash", response_model=Ok)
def empty_trash(db: DbSession, settings: AppSettings, user: CurrentUser, ip: ClientIp) -> Ok:
    """Purge every trashed site now, regardless of how long it has been there."""
    freed = 0
    purged = 0
    for row in trash.list_trash(db, settings):
        freed += trash.purge_site(db, settings, row.site)
        purged += 1
    audit.record(
        db,
        "trash.empty",
        actor=user.username,
        target=f"{purged} site(s)",
        ip=ip,
        detail={"freed_bytes": freed},
    )
    return Ok()
