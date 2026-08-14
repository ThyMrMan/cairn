"""Jobs and the SSE event streams (docs/09)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from cairn.api.deps import ClientIp, Csrf, CurrentUser, DbSession
from cairn.api.errors import ApiError
from cairn.api.schemas import JobsClear, JobsCleared, JobSummary, Ok, Page
from cairn.db.models import Job, Site
from cairn.db.types import utcnow
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


@router.get("/jobs/{job_id}/projection")
def job_projection(job_id: int, db: DbSession, _user: CurrentUser) -> dict[str, Any]:
    """Where a running crawl is heading.

    Deliberately does *not* invent a percentage. Nothing knows how many URLs a
    site has until the crawl has found them, and a progress bar built on the
    index's page estimate would have read "370% complete" on the crawl that
    prompted this — which is worse than no bar, because it looks like an
    answer. What can be said honestly is the rate, how far it is from its own
    cap, and how it compares with what the index expected; a reader draws
    "this is not converging" from those in a second.
    """
    job = db.get(Job, job_id)
    if job is None:
        raise ApiError("not_found", "That job does not exist.", status_code=404)

    progress = job.progress or {}
    urls = int(progress.get("done") or 0)
    started = job.started_at
    elapsed = (utcnow() - started).total_seconds() if started else 0.0
    per_minute = (urls / elapsed * 60) if elapsed > 0 and urls else 0.0

    cap: int | None = None
    estimate: int | None = None
    if job.site_id is not None:
        site = db.get(Site, job.site_id)
        if site is not None:
            cap = _scope_cap(db, site)
            estimate = _index_estimate(db, site)

    remaining = max(cap - urls, 0) if cap else None
    return {
        "running": job.status == "running",
        "urls": urls,
        "bytes": int(progress.get("bytes") or 0),
        "elapsed_s": round(elapsed, 1),
        "per_minute": round(per_minute, 1),
        "max_pages": cap,
        "remaining_to_cap": remaining,
        # None rather than 0 when it cannot be known, so the UI shows nothing
        # instead of "0s remaining".
        "eta_to_cap_s": round(remaining / per_minute * 60) if remaining and per_minute else None,
        "index_estimate": estimate,
    }


def _scope_cap(db: DbSession, site: Site) -> int | None:
    from cairn.services import sites as site_service

    try:
        return site_service.resolved_scope(db, site).max_pages
    except Exception:  # pragma: no cover — a broken scope must not break this
        return None


def _index_estimate(db: DbSession, site: Site) -> int | None:
    """What discovery thought the site was, for contrast.

    Pages, not URLs — the two are different quantities and the UI says so.
    Showing it anyway is the point: a crawl at four times the page estimate is
    either a site with a lot of images or a crawl in a hole, and the number
    that starts that thought is this one.
    """
    from cairn.services import discovery_service

    discovery = discovery_service.latest_discovery(db, site.id)
    if discovery is None:
        return None
    found = getattr(discovery, "urls_found", None)
    return int(found) if found else None


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


# ── clearing finished jobs ───────────────────────────────────────────────
#
# A run of failures is what a list of jobs looks like while something is being
# got working, and there was no way to clear them — every attempt stayed at
# the top of the page forever.
#
# Deleting a job row is the whole operation. Its events live in the in-memory
# bus, and nothing on disk is keyed by job id: an engine's log is written into
# the capture directory, and the job's temp directory is swept when it ends.
# The three tables that reference a job do so `ON DELETE SET NULL` with
# `PRAGMA foreign_keys=ON`, so a capture outlives the job that made it and
# simply stops naming it.

ACTIVE_STATUSES = ("queued", "running")


@router.delete("/jobs/{job_id}", response_model=Ok)
def delete_job(
    job_id: int,
    db: DbSession,
    user: CurrentUser,
    ip: ClientIp,
    request: Request,
) -> Ok:
    job = db.get(Job, job_id)
    if job is None:
        raise ApiError("not_found", "That job does not exist.", status_code=404)
    if job.status in ACTIVE_STATUSES:
        raise ApiError(
            "job_is_active",
            f"This job is {job.status}. Cancel it first — deleting the row would leave "
            "the process running with nothing watching it.",
            status_code=409,
        )
    db.delete(job)
    _bus(request).forget(job_id)
    audit.record(db, "job.delete", actor=user.username, target=str(job_id), ip=ip)
    return Ok()


@router.post("/jobs/clear", response_model=JobsCleared)
def clear_jobs(
    body: JobsClear,
    db: DbSession,
    user: CurrentUser,
    ip: ClientIp,
    request: Request,
) -> JobsCleared:
    """Delete finished jobs, optionally narrowed to one status, type or site.

    Never touches a queued or running job, whatever the filter says. Clearing
    a list is a tidying action and must not be a way to lose track of work
    that is still happening — so the guard is on the delete, not on the caller
    remembering to exclude them.
    """
    stmt = select(Job).where(Job.status.not_in(ACTIVE_STATUSES))
    if body.status:
        if body.status in ACTIVE_STATUSES:
            raise ApiError(
                "job_is_active",
                f"{body.status} jobs are still running; cancel them instead.",
                status_code=422,
            )
        stmt = stmt.where(Job.status == body.status)
    if body.type:
        stmt = stmt.where(Job.type == body.type)
    if body.site_id is not None:
        stmt = stmt.where(Job.site_id == body.site_id)

    jobs = db.scalars(stmt).all()
    bus = _bus(request)
    for job in jobs:
        bus.forget(job.id)
        db.delete(job)

    audit.record(
        db,
        "job.clear",
        actor=user.username,
        target=body.status or "finished",
        ip=ip,
        detail={"count": len(jobs), "type": body.type, "site_id": body.site_id},
    )
    return JobsCleared(deleted=len(jobs))


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
