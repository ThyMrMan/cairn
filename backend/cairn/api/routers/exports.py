"""WACZ export: build one, list what exists, download it, delete it (docs/09).

Exports are files in the site's `exports/` directory and nothing else — no
table, no rows to fall out of step with the disk. Listing is a directory read,
which is what makes an export copied in over the share appear in the UI, and
one deleted over the share disappear from it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import FileResponse

from cairn.api.deps import AppSettings, ClientIp, Csrf, CurrentUser, DbSession
from cairn.api.errors import ApiError
from cairn.api.schemas import ExportEntry, JobAccepted, Ok
from cairn.db.models import Capture, Site
from cairn.db.types import to_iso
from cairn.services import audit, storage, wacz
from cairn.services import sites as site_service

router = APIRouter(tags=["exports"], dependencies=[Csrf])

WACZ_MEDIA_TYPE = "application/wacz+zip"


def _supervisor(request: Request) -> Any:
    return request.app.state.supervisor


def _require_site(db: DbSession, site_id: int) -> Site:
    site = site_service.get_site(db, site_id)
    if site is None:
        raise ApiError("not_found", "That site does not exist.", status_code=404)
    return site


def _export_path(settings: AppSettings, site: Site, name: str) -> Path:
    """Resolve a name inside the site's exports directory, or refuse.

    The name comes from a URL, so it is attacker-controlled in the same sense
    every path in this application is: resolved with symlinks followed and
    refused if it leaves the directory (docs/11).
    """
    try:
        path = storage.resolve_within(wacz.exports_dir(settings, site.archive_path), name)
    except storage.StoragePathError as exc:
        raise ApiError("not_found", "No such export.", status_code=404) from exc
    if not path.is_file():
        raise ApiError("not_found", "No such export.", status_code=404)
    return path


@router.get("/sites/{site_id}/exports", response_model=list[ExportEntry])
def list_exports(
    site_id: int, db: DbSession, settings: AppSettings, _user: CurrentUser
) -> list[ExportEntry]:
    site = _require_site(db, site_id)
    directory = wacz.exports_dir(settings, site.archive_path)
    if not directory.is_dir():
        return []
    from datetime import UTC, datetime

    entries = []
    for path in sorted(directory.glob("*.wacz"), reverse=True):
        stat = path.stat()
        entries.append(
            ExportEntry(
                name=path.name,
                size_bytes=stat.st_size,
                created_at=to_iso(datetime.fromtimestamp(stat.st_mtime, tz=UTC)),
            )
        )
    return entries


@router.post(
    "/sites/{site_id}/export/wacz",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def export_site(
    site_id: int,
    db: DbSession,
    user: CurrentUser,
    ip: ClientIp,
    supervisor: Annotated[Any, Depends(_supervisor)],
) -> JobAccepted:
    """Package every capture of a site into one `.wacz`."""
    site = _require_site(db, site_id)
    job = supervisor.enqueue(
        db,
        job_type="export",
        site_id=site.id,
        spec={"filename": wacz.export_name(site.slug)},
        priority=200,
    )
    audit.record(
        db, "export.wacz", actor=user.username, target=site.slug, ip=ip, detail={"job_id": job.id}
    )
    db.commit()
    supervisor.notify()
    return JobAccepted(job_id=job.id)


@router.post(
    "/captures/{capture_id}/export/wacz",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def export_capture(
    capture_id: int,
    db: DbSession,
    user: CurrentUser,
    ip: ClientIp,
    supervisor: Annotated[Any, Depends(_supervisor)],
) -> JobAccepted:
    capture = db.get(Capture, capture_id)
    if capture is None:
        raise ApiError("not_found", "That capture does not exist.", status_code=404)
    site = _require_site(db, capture.site_id)
    job = supervisor.enqueue(
        db,
        job_type="export",
        site_id=site.id,
        spec={
            "capture_dirs": [capture.dir_name],
            "filename": wacz.export_name(f"{site.slug}-{capture.dir_name}"),
        },
        priority=200,
    )
    audit.record(
        db,
        "export.wacz",
        actor=user.username,
        target=f"{site.slug}/{capture.dir_name}",
        ip=ip,
        detail={"job_id": job.id},
    )
    db.commit()
    supervisor.notify()
    return JobAccepted(job_id=job.id)


@router.get("/sites/{site_id}/exports/{name}")
def download_export(
    site_id: int,
    name: str,
    db: DbSession,
    settings: AppSettings,
    _user: CurrentUser,
) -> FileResponse:
    site = _require_site(db, site_id)
    path = _export_path(settings, site, name)
    # Always an attachment. A WACZ is a zip of untrusted archived bytes and
    # must never be offered to the browser as something to render.
    return FileResponse(
        path,
        media_type=WACZ_MEDIA_TYPE,
        filename=path.name,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.delete("/sites/{site_id}/exports/{name}", response_model=Ok)
def delete_export(
    site_id: int,
    name: str,
    db: DbSession,
    settings: AppSettings,
    user: CurrentUser,
    ip: ClientIp,
) -> Ok:
    site = _require_site(db, site_id)
    path = _export_path(settings, site, name)
    path.unlink()
    audit.record(db, "export.delete", actor=user.username, target=f"{site.slug}/{name}", ip=ip)
    return Ok()


@router.get("/sites/{site_id}/exports/{name}/verify")
def verify_export(
    site_id: int,
    name: str,
    db: DbSession,
    settings: AppSettings,
    _user: CurrentUser,
    _full: Annotated[bool, Query(alias="full")] = False,
) -> dict[str, Any]:
    """Re-read an export: checksums, and every index entry resolving.

    The second half is the one worth having. A zip whose checksums agree can
    still replay nothing, because what makes a WACZ work is that the offset
    the index records lands on the record it names.
    """
    site = _require_site(db, site_id)
    path = _export_path(settings, site, name)
    check = wacz.verify(path)
    return {
        "ok": check.ok,
        "problems": check.problems,
        "records": check.records,
        "resources": check.resources,
    }
