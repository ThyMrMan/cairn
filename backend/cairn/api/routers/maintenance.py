"""Trash and the repair actions (docs/09).

Everything here operates on derived or deleted data, which is why it is
separate from the routers that create things. The two rebuild endpoints are
the answer to "the tree looks wrong": both regenerate from the database, both
are safe to run at any time, and neither can lose an archive.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status

from cairn.api.deps import AppSettings, ClientIp, Csrf, CurrentUser, DbSession
from cairn.api.errors import ApiError
from cairn.api.schemas import JobAccepted, Ok, ThumbnailSettings, TrashEntry
from cairn.db.models import Folder
from cairn.services import audit, replay, symlinks, trash

router = APIRouter(tags=["maintenance"], dependencies=[Csrf])


def _supervisor(request: Request) -> Any:
    return request.app.state.supervisor


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


@router.post(
    "/maintenance/verify", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED
)
def verify_archive(
    db: DbSession,
    user: CurrentUser,
    ip: ClientIp,
    supervisor: Annotated[Any, Depends(_supervisor)],
    site_id: Annotated[int | None, Query()] = None,
    deep: Annotated[bool, Query()] = False,
) -> JobAccepted:
    """Re-checksum the archive against what each capture recorded.

    A job rather than a request: it reads every archived byte, which on a NAS
    array is minutes to hours and spins up disks. `deep` additionally parses
    each WARC end to end, which reads everything twice.
    """
    job = supervisor.enqueue(
        db, job_type="verify", site_id=site_id, spec={"deep": deep}, priority=200
    )
    audit.record(
        db,
        "maintenance.verify",
        actor=user.username,
        target=str(site_id) if site_id else "all sites",
        ip=ip,
        detail={"job_id": job.id, "deep": deep},
    )
    db.commit()
    supervisor.notify()
    return JobAccepted(job_id=job.id)


@router.get("/thumbnails/settings", response_model=ThumbnailSettings)
def thumbnail_settings(db: DbSession, _user: CurrentUser) -> ThumbnailSettings:
    from cairn.services import thumbnail

    return ThumbnailSettings(enabled=thumbnail.enabled(db))


@router.put("/thumbnails/settings", response_model=ThumbnailSettings)
def put_thumbnail_settings(
    body: ThumbnailSettings, db: DbSession, _user: CurrentUser
) -> ThumbnailSettings:
    from cairn.services import settings_store, thumbnail

    settings_store.put(db, thumbnail.ENABLED_SETTING, body.enabled)
    db.commit()
    return body


@router.post(
    "/maintenance/thumbnails", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED
)
def rebuild_thumbnails(
    db: DbSession,
    user: CurrentUser,
    ip: ClientIp,
    supervisor: Annotated[Any, Depends(_supervisor)],
    site_id: Annotated[int | None, Query()] = None,
    force: Annotated[bool, Query()] = False,
) -> JobAccepted:
    """Photograph archives captured before there was anything to photograph with.

    A job because each site is a browser page load: fine for one, minutes for
    two hundred. `force` re-takes pictures that already exist, which is what
    you want after changing nothing about the archive and everything about the
    replay — a pywb upgrade, say.
    """
    job = supervisor.enqueue(
        db, job_type="thumbnail", site_id=site_id, spec={"force": force}, priority=250
    )
    audit.record(
        db,
        "maintenance.thumbnails",
        actor=user.username,
        target=str(site_id) if site_id else "all sites",
        ip=ip,
        detail={"job_id": job.id, "force": force},
    )
    db.commit()
    supervisor.notify()
    return JobAccepted(job_id=job.id)


# ── the copy ─────────────────────────────────────────────────────────────
#
# Making the copy is rsync's job, or restic's. Knowing the copy is good is
# this instance's, because it holds the checksums taken when the bytes were
# written — see `services/mirror.py`.


@router.get("/mirror")
def survey_mirror(
    db: DbSession,
    settings: AppSettings,
    _user: CurrentUser,
    path: Annotated[str, Query(min_length=1, max_length=1024)],
) -> dict[str, Any]:
    """Which captures the copy has, and which it does not. A listing, not a read."""
    from cairn.services import mirror

    try:
        root = mirror.require_root(settings, path)
    except mirror.MirrorError as exc:
        raise ApiError("bad_path", str(exc), status_code=400) from exc
    return mirror.survey(db, settings, root).to_dict()


@router.post("/mirror/verify", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
def verify_mirror(
    db: DbSession,
    settings: AppSettings,
    user: CurrentUser,
    ip: ClientIp,
    supervisor: Annotated[Any, Depends(_supervisor)],
    path: Annotated[str, Query(min_length=1, max_length=1024)],
    deep: Annotated[bool, Query()] = False,
) -> JobAccepted:
    """Re-checksum the copy against what each capture recorded here.

    The expensive question, and the one rsync cannot answer: it reports that
    it transferred bytes, not that the archive in the copy is complete and
    unmodified.
    """
    from cairn.services import mirror

    try:
        root = mirror.require_root(settings, path)
    except mirror.MirrorError as exc:
        raise ApiError("bad_path", str(exc), status_code=400) from exc

    job = supervisor.enqueue(
        db,
        job_type="verify",
        site_id=None,
        spec={"deep": deep, "root": str(root)},
        priority=200,
    )
    audit.record(db, "maintenance.verify-mirror", actor=user.username, target=str(root), ip=ip)
    db.commit()
    supervisor.notify()
    return JobAccepted(job_id=job.id)


@router.get("/maintenance/integrity")
def integrity_report(db: DbSession, settings: AppSettings, _user: CurrentUser) -> dict[str, Any]:
    """Archive health: what was verified, when, and what is still unchecked."""
    from cairn.services import integrity

    return integrity.health(db, settings)


@router.get("/site-health")
def site_health(db: DbSession, _user: CurrentUser) -> dict[str, Any]:
    """Which archived sites are still live, and which are not."""
    from cairn.services import sitehealth

    return sitehealth.summary(db)


@router.post("/sites/{site_id}/health-check")
async def check_one_site(
    site_id: int, db: DbSession, user: CurrentUser, ip: ClientIp
) -> dict[str, Any]:
    """Ask now, rather than waiting for the sweep.

    Runs the same code the ticker does, including the confirmation counter —
    so pressing this twice can change a state, and pressing it once cannot.
    That is deliberate: a button that reported "gone" on one bad response
    would be a different feature from the one the sweep implements, and the
    two disagreeing is worse than either.
    """
    from cairn.discovery.fetch import USER_AGENT, Fetcher
    from cairn.services import audit as audit_service
    from cairn.services import sitehealth
    from cairn.services import sites as site_service

    site = site_service.get_site(db, site_id)
    if site is None:
        raise ApiError("not_found", "That site does not exist.", status_code=404)

    seed = site.seed_url
    async with Fetcher(user_agent=USER_AGENT) as fetcher:
        found = await sitehealth.probe(fetcher, seed)
    changed = sitehealth.record(db, site, found)
    audit_service.record(db, "site.health", actor=user.username, target=site.slug, ip=ip)
    db.commit()

    return {
        "state": found.state,
        "changed": changed,
        "http_status": found.http_status,
        "final_url": found.final_url,
        "error": found.error,
        "message": sitehealth.describe(
            found.state, status=found.http_status, final_url=found.final_url
        ),
        "health": sitehealth.for_site(db, site_id),
    }


@router.get("/digest")
def digest_report(
    db: DbSession,
    settings: AppSettings,
    _user: CurrentUser,
    days: int = Query(7, ge=1, le=365),
) -> dict[str, Any]:
    """The periodic report, on demand.

    The same thing the scheduler pushes. Readable without configuring a
    notification target, because a report nobody has set up a webhook for is a
    report nobody ever reads — and this one is mostly about what has *not*
    happened, which no other page in the app answers.
    """
    from datetime import timedelta

    from cairn.db.types import utcnow
    from cairn.services import digest as digest_service

    now = utcnow()
    report = digest_service.build(
        db,
        settings,
        since=now - timedelta(days=days),
        now=now,
        previous_total=digest_service.previous_total(db),
    )
    return {**report.to_dict(), "text": digest_service.render_text(report)}


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
