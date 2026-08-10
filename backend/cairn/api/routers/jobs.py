"""Jobs and the SSE event streams (docs/09)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from cairn.api.deps import ClientIp, Csrf, CurrentUser, DbSession
from cairn.api.errors import ApiError
from cairn.api.schemas import JobSummary, Ok, Page
from cairn.db.models import Job, Site
from cairn.services import audit
from cairn.services.events import EV_STATUS, BusEvent, EventBus, format_sse

router = APIRouter(tags=["jobs"], dependencies=[Csrf])

# An SSE response must not be buffered by an intermediary, or the live log
# arrives in one lump when the crawl finishes.
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _bus(request: Request) -> EventBus:
    bus: EventBus = request.app.state.bus
    return bus


def _supervisor(request: Request) -> Any:
    return request.app.state.supervisor


def _summary(db: DbSession, job: Job) -> JobSummary:
    title = None
    if job.site_id is not None:
        site = db.get(Site, job.site_id)
        title = site.title if site else None
    return JobSummary(
        id=job.id,
        type=job.type,
        site_id=job.site_id,
        site_title=title,
        status=job.status,
        progress=job.progress,
        queued_at=job.queued_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        error=job.error,
        attempts=job.attempts,
    )


@router.get("/jobs", response_model=Page[JobSummary])
def list_jobs(
    db: DbSession,
    _user: CurrentUser,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    job_type: Annotated[str | None, Query(alias="type")] = None,
    site_id: int | None = None,
    active: bool = False,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
) -> Page[JobSummary]:
    stmt = select(Job)
    if status_filter:
        stmt = stmt.where(Job.status == status_filter)
    if active:
        stmt = stmt.where(Job.status.in_(("queued", "running")))
    if job_type:
        stmt = stmt.where(Job.type == job_type)
    if site_id is not None:
        stmt = stmt.where(Job.site_id == site_id)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(Job.id.desc()).limit(per_page).offset((page - 1) * per_page)
    ).all()
    return Page[JobSummary](
        items=[_summary(db, j) for j in rows], total=total, page=page, per_page=per_page
    )


@router.get("/jobs/{job_id}", response_model=JobSummary)
def get_job(job_id: int, db: DbSession, _user: CurrentUser) -> JobSummary:
    job = db.get(Job, job_id)
    if job is None:
        raise ApiError("not_found", "That job does not exist.", status_code=404)
    return _summary(db, job)


@router.post("/jobs/{job_id}/cancel", response_model=Ok)
async def cancel_job(
    job_id: int,
    db: DbSession,
    user: CurrentUser,
    ip: ClientIp,
    supervisor: Annotated[Any, Depends(_supervisor)],
) -> Ok:
    job = db.get(Job, job_id)
    if job is None:
        raise ApiError("not_found", "That job does not exist.", status_code=404)
    if job.status not in ("queued", "running"):
        raise ApiError("not_cancellable", f"This job is already {job.status}.", status_code=409)
    audit.record(db, audit.CAPTURE_CANCEL, actor=user.username, target=str(job_id), ip=ip)
    db.commit()

    # SIGTERM, then a grace period: the engine's contract is to close and
    # flush its WARC, so cancelling costs a partial capture rather than a
    # truncated file (docs/05).
    if not await supervisor.cancel(job_id):
        raise ApiError("not_cancellable", "That job could not be cancelled.", status_code=409)
    return Ok()


# ── SSE ──────────────────────────────────────────────────────────────────


TERMINAL_STATUSES = frozenset({"ok", "partial", "failed", "cancelled", "interrupted"})


def _is_terminal(event: BusEvent) -> bool:
    return event.event == EV_STATUS and event.data.get("status") in TERMINAL_STATUSES


async def _event_stream(
    request: Request,
    bus: EventBus,
    job_id: int | None,
    last_event_id: int,
    *,
    already_finished: bool = False,
) -> AsyncIterator[str]:
    """Replay what the client missed, then follow along until the job ends.

    Closing at the end matters as much as opening: a stream that replays a
    finished job's history and then waits forever holds a connection and a
    server task open for every completed job anyone looks at, and the client
    has no way to know nothing more is coming.
    """
    async with bus.subscribe(job_id) as sub:
        if job_id is not None:
            if bus.has_gap(job_id, last_event_id):
                yield format_sse(
                    BusEvent(
                        id=last_event_id,
                        job_id=job_id,
                        event="lagged",
                        data={"message": "Older events are no longer buffered."},
                    )
                )
            for past in bus.history(job_id, last_event_id):
                yield format_sse(past)
                if _is_terminal(past):
                    return

            if already_finished:
                # Terminal before this request arrived, and its events have
                # aged out of the ring buffer. Say so rather than hanging.
                yield format_sse(
                    BusEvent(
                        id=last_event_id,
                        job_id=job_id,
                        event=EV_STATUS,
                        data={"status": "finished", "replayed": True},
                    )
                )
                return

        async for item in bus.stream(sub):
            if await request.is_disconnected():
                break
            yield format_sse(item)
            if item is not None and _is_terminal(item):
                break


def _last_event_id(request: Request) -> int:
    raw = request.headers.get("Last-Event-ID", "") or request.query_params.get("last_event_id", "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


@router.get("/jobs/{job_id}/events")
async def job_events(
    job_id: int,
    request: Request,
    db: DbSession,
    _user: CurrentUser,
    bus: Annotated[EventBus, Depends(_bus)],
) -> StreamingResponse:
    job = db.get(Job, job_id)
    if job is None:
        raise ApiError("not_found", "That job does not exist.", status_code=404)
    return StreamingResponse(
        _event_stream(
            request,
            bus,
            job_id,
            _last_event_id(request),
            already_finished=job.status not in ("queued", "running"),
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.get("/events")
async def all_events(
    request: Request,
    _user: CurrentUser,
    bus: Annotated[EventBus, Depends(_bus)],
) -> StreamingResponse:
    """Global firehose for the activity sidebar."""
    return StreamingResponse(
        _event_stream(request, bus, None, 0),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
