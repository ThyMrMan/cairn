"""Captures: detail, log, URL list (docs/09)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, or_, select

from cairn.api.deps import AppSettings, ClientIp, Csrf, CurrentUser, DbSession
from cairn.api.errors import ApiError
from cairn.api.schemas import CaptureDetail, CaptureUrlEntry, Ok, Page
from cairn.db.models import Capture, CaptureUrl, Site
from cairn.services import audit, search, storage, textextract

router = APIRouter(tags=["captures"], dependencies=[Csrf])

LOG_TAIL_DEFAULT = 500
LOG_TAIL_MAX = 20_000


def _require_capture(db: DbSession, capture_id: int) -> Capture:
    capture = db.get(Capture, capture_id)
    if capture is None:
        raise ApiError("not_found", "That capture does not exist.", status_code=404)
    return capture


def _capture_dir(settings: AppSettings, db: DbSession, capture: Capture):  # type: ignore[no-untyped-def]
    site = db.get(Site, capture.site_id)
    if site is None:  # pragma: no cover — FK guarantees it
        raise ApiError("not_found", "That capture's site is gone.", status_code=404)
    return storage.site_dir(settings, site.archive_path) / storage.CAPTURES_DIR / capture.dir_name


@router.get("/captures/{capture_id}", response_model=CaptureDetail)
def get_capture(
    capture_id: int, db: DbSession, settings: AppSettings, _user: CurrentUser
) -> CaptureDetail:
    capture = _require_capture(db, capture_id)
    manifest: dict[str, Any] | None = None
    path = _capture_dir(settings, db, capture) / storage.MANIFEST_FILE
    if path.is_file():
        try:
            manifest = storage.read_json(path)
        except (OSError, ValueError):
            manifest = None

    return CaptureDetail(
        id=capture.id,
        site_id=capture.site_id,
        job_id=capture.job_id,
        kind=capture.kind,
        engine_id=capture.engine_id,
        engine_version=capture.engine_version,
        dir_name=capture.dir_name,
        status=capture.status,
        started_at=capture.started_at,
        finished_at=capture.finished_at,
        url_count=capture.url_count,
        error_count=capture.error_count,
        bytes_written=capture.bytes_written,
        artifacts=list(capture.warc_files or []),
        manifest=manifest,
    )


@router.get("/captures/{capture_id}/log", response_class=PlainTextResponse)
def capture_log(
    capture_id: int,
    db: DbSession,
    settings: AppSettings,
    _user: CurrentUser,
    tail: int = Query(LOG_TAIL_DEFAULT, ge=1, le=LOG_TAIL_MAX),
) -> PlainTextResponse:
    """The crawl log, last `tail` lines.

    Read from the end rather than loaded whole: a long crawl's log runs to
    hundreds of megabytes and the UI only ever shows the tail.
    """
    capture = _require_capture(db, capture_id)
    path = _capture_dir(settings, db, capture) / "crawl.log"
    if not path.is_file():
        return PlainTextResponse("", status_code=200)

    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            # ~200 bytes per line, with a floor so short logs are read whole.
            window = min(size, max(64 * 1024, tail * 300))
            fh.seek(size - window)
            chunk = fh.read()
    except OSError as exc:  # pragma: no cover
        raise ApiError("io_error", f"Could not read the log: {exc}", status_code=500) from exc

    lines = chunk.decode("utf-8", errors="replace").splitlines()
    if window < size and lines:
        lines = lines[1:]  # the first line is probably a fragment
    return PlainTextResponse("\n".join(lines[-tail:]))


@router.get("/captures/{capture_id}/urls", response_model=Page[CaptureUrlEntry])
def capture_urls(
    capture_id: int,
    db: DbSession,
    _user: CurrentUser,
    errors_only: bool = False,
    q: str | None = None,
    host: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=500),
) -> Page[CaptureUrlEntry]:
    capture = _require_capture(db, capture_id)
    stmt = select(CaptureUrl).where(CaptureUrl.capture_id == capture.id)
    if errors_only:
        stmt = stmt.where(or_(CaptureUrl.status_code >= 400, CaptureUrl.error.isnot(None)))
    if host:
        stmt = stmt.where(CaptureUrl.host == host)
    if q:
        stmt = stmt.where(CaptureUrl.url.ilike(f"%{q.strip()}%"))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(CaptureUrl.id).limit(per_page).offset((page - 1) * per_page)
    ).all()
    return Page[CaptureUrlEntry](
        items=[
            CaptureUrlEntry(
                id=r.id,
                url=r.url,
                host=r.host,
                status_code=r.status_code,
                mime=r.mime,
                size_bytes=r.size_bytes,
                is_revisit=r.is_revisit,
                fetched_at=r.fetched_at,
                error=r.error,
            )
            for r in rows
        ],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.delete("/captures/{capture_id}", response_model=Ok)
def delete_capture(
    capture_id: int,
    db: DbSession,
    settings: AppSettings,
    user: CurrentUser,
    ip: ClientIp,
    force: Annotated[bool, Query()] = False,
) -> Ok:
    """Delete a capture and its directory.

    Refuses to remove the only capture unless forced: deleting the last one
    leaves a site row pointing at an empty archive, which reads as data loss
    even when it was deliberate.
    """
    capture = _require_capture(db, capture_id)
    if capture.status == "running":
        raise ApiError(
            "capture_running", "Cancel the job before deleting this capture.", status_code=409
        )

    siblings = (
        db.scalar(select(func.count(Capture.id)).where(Capture.site_id == capture.site_id)) or 0
    )
    if siblings <= 1 and not force:
        raise ApiError(
            "last_capture",
            "This is the site's only capture. Pass force=true to delete it anyway.",
            status_code=409,
        )

    directory = _capture_dir(settings, db, capture)
    site = db.get(Site, capture.site_id)
    # Explicitly, before the row goes: the FTS index has no foreign key to
    # cascade through, so its rows would outlive the capture that put them
    # there. Harmless in results — the join drops them — and a leak that grows
    # with every deleted capture.
    search.drop_capture(db, capture.id)
    if site is not None:
        textextract.remove_capture_text(settings, site.archive_path, capture.dir_name)
    db.delete(capture)
    db.flush()

    if directory.is_dir():
        import shutil

        try:
            shutil.rmtree(directory)
        except OSError as exc:  # pragma: no cover
            raise ApiError(
                "io_error", f"Removed the record but not the files: {exc}", status_code=500
            ) from exc

    audit.record(
        db,
        audit.CAPTURE_DELETE,
        actor=user.username,
        target=site.slug if site else str(capture.site_id),
        ip=ip,
        detail={"capture_id": capture_id},
    )
    return Ok()
