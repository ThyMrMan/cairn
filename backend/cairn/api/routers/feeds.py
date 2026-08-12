"""Feeds, watchers and the scheduler's settings (docs/09).

The shape follows docs/08's insistence on observability: every read here
answers "what has this actually been doing", and every write is something a
person can undo. `POST /feeds/{id}/poll` runs the same code path the ticker
runs, so what somebody sees when they press the button is what happens
unattended — a scheduler you can only observe indirectly is one you end up not
believing.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, status
from sqlalchemy.orm import Session

from cairn.api.deps import AppSettings, ClientIp, Csrf, CurrentUser, DbSession
from cairn.api.errors import ApiError
from cairn.api.schemas import (
    FeedCandidateModel,
    FeedCreate,
    FeedItemEntry,
    FeedPollEntry,
    FeedPollResult,
    FeedSummary,
    FeedTestRequest,
    FeedUpdate,
    NotifySettings,
    NotifyTarget,
    NotifyUpdate,
    Ok,
    QuietHours,
    ScheduleSettings,
)
from cairn.db.models import Feed, FeedItem, Site
from cairn.services import audit, notify, settings_store
from cairn.services import digest as digest_service
from cairn.services import feeds as feed_service
from cairn.services import scheduler as scheduler_service
from cairn.services import sites as site_service

router = APIRouter(tags=["feeds"], dependencies=[Csrf])


def _scheduler(request: Request) -> Any:
    return request.app.state.scheduler


def _site(db: Session, site_id: int) -> Site:
    site = db.get(Site, site_id)
    if site is None or site.deleted_at is not None:
        raise ApiError("not_found", "No such site.", status_code=status.HTTP_404_NOT_FOUND)
    return site


def _feed(db: Session, feed_id: int) -> Feed:
    feed = db.get(Feed, feed_id)
    if feed is None:
        raise ApiError("not_found", "No such feed.", status_code=status.HTTP_404_NOT_FOUND)
    return feed


def _summary(db: Session, feed: Feed) -> FeedSummary:
    return FeedSummary(
        id=feed.id,
        site_id=feed.site_id,
        url=feed.url,
        kind=feed.kind,
        title=feed.title,
        enabled=feed.enabled,
        auto_capture=feed.auto_capture,
        recapture_on_update=feed.recapture_on_update,
        interval_min=feed.interval_min,
        next_poll_at=feed.next_poll_at,
        last_polled_at=feed.last_polled_at,
        last_success_at=feed.last_success_at,
        last_status=feed.last_status,
        consecutive_failures=feed.consecutive_failures,
        last_error=feed.last_error,
        disabled_reason=feed.disabled_reason,
        counts=feed_service.counts_for(db, feed.id),
    )


# ── per site ─────────────────────────────────────────────────────────────


@router.get("/sites/{site_id}/feeds", response_model=list[FeedSummary])
def list_feeds(site_id: int, db: DbSession, _user: CurrentUser) -> list[FeedSummary]:
    site = _site(db, site_id)
    return [_summary(db, feed) for feed in sorted(site.feeds, key=lambda f: f.id)]


@router.post("/sites/{site_id}/feeds", response_model=FeedSummary, status_code=201)
def add_feed(
    site_id: int, body: FeedCreate, db: DbSession, user: CurrentUser, ip: ClientIp
) -> FeedSummary:
    site = _site(db, site_id)
    try:
        feed = feed_service.add_feed(
            db,
            site,
            url=body.url,
            kind=body.kind,
            interval_min=body.interval_min,
            enabled=body.enabled,
            auto_capture=body.auto_capture,
            title=body.title,
        )
    except feed_service.FeedError as exc:
        raise ApiError("invalid_feed", str(exc), status_code=400) from exc
    audit.record(db, "feed.add", actor=user.username, target=feed.url, ip=ip)
    return _summary(db, feed)


@router.post("/sites/{site_id}/feeds/discover", response_model=list[FeedCandidateModel])
async def discover_feeds(
    site_id: int, db: DbSession, settings: AppSettings, _user: CurrentUser
) -> list[FeedCandidateModel]:
    """Everything worth watching on this site, probed live.

    Nothing is saved. docs/08 asks for a checklist rather than silent adoption,
    and this is the checklist: each row already knows whether its entries are
    inside the site's scope.
    """
    from cairn.discovery.fetch import Fetcher
    from cairn.discovery.platform import PRESETS
    from cairn.services import discovery_service

    site = _site(db, site_id)
    scope = site_service.resolved_scope(db, site)
    latest = discovery_service.latest_discovery(db, site.id)
    summary: dict[str, Any] = (latest.summary or {}) if latest is not None else {}
    platform = str((summary.get("fingerprint") or {}).get("platform") or "")
    preset = PRESETS.get(platform)

    async with Fetcher() as fetcher:
        candidates = await feed_service.discover(
            site.seed_url, fetcher=fetcher, preset=preset, scope=scope
        )
    known = {feed_service.canonical_url(f.url) for f in site.feeds}
    return [
        FeedCandidateModel(**candidate.to_dict())
        for candidate in candidates
        if feed_service.canonical_url(candidate.url) not in known
    ]


@router.post("/sites/{site_id}/feeds/test", response_model=FeedCandidateModel)
async def test_feed(
    site_id: int, body: FeedTestRequest, db: DbSession, _user: CurrentUser
) -> FeedCandidateModel:
    """Fetch a candidate URL and report what it is, before anything is saved."""
    from cairn.discovery.fetch import Fetcher

    site = _site(db, site_id)
    scope = site_service.resolved_scope(db, site)
    async with Fetcher() as fetcher:
        candidate = await feed_service.probe(body.url, fetcher=fetcher, kind=body.kind, scope=scope)
    return FeedCandidateModel(**candidate.to_dict())


# ── one feed ─────────────────────────────────────────────────────────────


@router.patch("/feeds/{feed_id}", response_model=FeedSummary)
def update_feed(
    feed_id: int, body: FeedUpdate, db: DbSession, user: CurrentUser, ip: ClientIp
) -> FeedSummary:
    feed = _feed(db, feed_id)
    if body.title is not None:
        feed.title = body.title
    if body.interval_min is not None:
        feed.interval_min = max(body.interval_min, feed_service.MIN_INTERVAL_MIN)
        # A shortened interval should take effect now, not after the old one
        # has elapsed — otherwise setting six hours to one leaves the feed
        # apparently ignoring the change for the next five.
        feed.next_poll_at = feed_service.next_due(feed)
    if body.auto_capture is not None:
        feed.auto_capture = body.auto_capture
    if body.recapture_on_update is not None:
        feed.recapture_on_update = body.recapture_on_update
    if body.enabled is not None:
        feed.enabled = body.enabled
        if body.enabled:
            # Re-enabling clears the backoff as well as the reason. A feed
            # switched back on after a fix should try immediately; leaving it
            # at 10 failures would mean a day before the next attempt.
            feed.disabled_reason = None
            feed.consecutive_failures = 0
            feed.last_error = None
            feed.next_poll_at = None
    audit.record(db, "feed.update", actor=user.username, target=feed.url, ip=ip)
    return _summary(db, feed)


@router.delete("/feeds/{feed_id}", response_model=Ok)
def delete_feed(feed_id: int, db: DbSession, user: CurrentUser, ip: ClientIp) -> Ok:
    feed = _feed(db, feed_id)
    audit.record(db, "feed.delete", actor=user.username, target=feed.url, ip=ip)
    db.delete(feed)
    return Ok()


@router.post("/feeds/{feed_id}/poll", response_model=FeedPollResult)
async def poll_feed(
    feed_id: int, request: Request, db: DbSession, _user: CurrentUser
) -> FeedPollResult:
    """Poll now, and capture anything it turns up.

    Runs the ticker's own code, including the capture it would have enqueued —
    with one difference: quiet hours do not apply, because they are a rule
    about unattended work and this was a button press.
    """
    _feed(db, feed_id)
    # The scheduler opens its own sessions and commits there. This one has an
    # open read transaction whose snapshot predates that, so it is ended here
    # rather than left to serve stale rows back to the response.
    db.rollback()

    outcome = await _scheduler(request).poll(feed_id)
    if outcome is None:
        raise ApiError("not_found", "No such feed.", status_code=status.HTTP_404_NOT_FOUND)

    job_ids: list[int] = []
    fresh = _feed(db, feed_id)
    if outcome.new_items and fresh.auto_capture:
        job_ids = _scheduler(request).capture_pending(db, fresh)
    return FeedPollResult(
        status=outcome.status,
        action=outcome.action,
        entries_seen=outcome.entries_seen,
        new_items=len(outcome.new_items),
        gone_items=len(outcome.gone_items),
        baseline=outcome.baseline,
        error=outcome.error,
        job_ids=job_ids,
    )


@router.post("/feeds/{feed_id}/capture", response_model=FeedPollResult)
def capture_pending(
    feed_id: int, request: Request, db: DbSession, _user: CurrentUser
) -> FeedPollResult:
    """Capture whatever is already pending, without polling first."""
    feed = _feed(db, feed_id)
    job_ids = _scheduler(request).capture_pending(db, feed)
    return FeedPollResult(
        status=0,
        action=f"queued {len(job_ids)} capture job(s)",
        entries_seen=0,
        new_items=0,
        gone_items=0,
        baseline=False,
        error=None,
        job_ids=job_ids,
    )


@router.get("/feeds/{feed_id}/polls", response_model=list[FeedPollEntry])
def poll_history(
    feed_id: int,
    db: DbSession,
    _user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[FeedPollEntry]:
    _feed(db, feed_id)
    return [
        FeedPollEntry(
            id=row.id,
            ts=row.ts,
            status=row.status,
            duration_ms=row.duration_ms,
            entries_seen=row.entries_seen,
            new_items=row.new_items,
            gone_items=row.gone_items,
            action=row.action,
            error=row.error,
        )
        for row in feed_service.history(db, feed_id, limit=limit)
    ]


@router.get("/feeds/{feed_id}/items", response_model=list[FeedItemEntry])
def feed_items(
    feed_id: int,
    db: DbSession,
    _user: CurrentUser,
    item_status: Annotated[str | None, Query(alias="status", max_length=16)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[FeedItemEntry]:
    from sqlalchemy import select

    _feed(db, feed_id)
    query = select(FeedItem).where(FeedItem.feed_id == feed_id)
    if item_status:
        query = query.where(FeedItem.status == item_status)
    rows = db.scalars(query.order_by(FeedItem.id.desc()).limit(limit)).all()
    return [
        FeedItemEntry(
            id=row.id,
            url=row.url,
            title=row.title,
            status=row.status,
            published_at=row.published_at,
            first_seen_at=row.first_seen_at,
            gone_at=row.gone_at,
            capture_id=row.capture_id,
        )
        for row in rows
    ]


# ── settings ─────────────────────────────────────────────────────────────


@router.get("/schedule", response_model=ScheduleSettings)
def read_schedule(db: DbSession, _user: CurrentUser) -> ScheduleSettings:
    raw: Any = settings_store.get(db, scheduler_service.QUIET_HOURS_SETTING, {})
    quiet = QuietHours(**raw) if isinstance(raw, dict) else QuietHours()
    return ScheduleSettings(
        quiet_hours=quiet,
        per_host_serial=bool(settings_store.get(db, "jobs.per_host_serial", True)),
        full_recapture_days=settings_store.get_int(db, scheduler_service.RECAPTURE_SETTING, 0),
        in_quiet_hours_now=scheduler_service.in_quiet_hours(db),
        digest_every_days=digest_service.every_days(db),
    )


@router.put("/schedule", response_model=ScheduleSettings)
def write_schedule(
    body: ScheduleSettings, db: DbSession, user: CurrentUser, ip: ClientIp
) -> ScheduleSettings:
    settings_store.put(db, scheduler_service.QUIET_HOURS_SETTING, body.quiet_hours.model_dump())
    settings_store.put(db, "jobs.per_host_serial", body.per_host_serial)
    settings_store.put(db, scheduler_service.RECAPTURE_SETTING, body.full_recapture_days)
    settings_store.put(db, digest_service.EVERY_DAYS_SETTING, body.digest_every_days)
    audit.record(
        db,
        "settings.schedule",
        actor=user.username,
        ip=ip,
        detail={"recapture_days": body.full_recapture_days},
    )
    return read_schedule(db, user)


@router.get("/notifications", response_model=NotifySettings)
def read_notifications(db: DbSession, _user: CurrentUser) -> NotifySettings:
    return NotifySettings(
        targets=[
            NotifyTarget(url=t.url, enabled=t.enabled, label=t.label) for t in notify.targets(db)
        ],
        events={event: notify.wants(db, event) for event in notify.EVENTS},
        labels={event: label for event, (label, _on) in notify.EVENTS.items()},
        apprise_available=notify.apprise_available(),
    )


@router.put("/notifications", response_model=NotifySettings)
def write_notifications(
    body: NotifyUpdate, db: DbSession, user: CurrentUser, ip: ClientIp
) -> NotifySettings:
    if body.targets is not None:
        notify.set_targets(db, [t.model_dump() for t in body.targets])
    for event, wanted in (body.events or {}).items():
        if event in notify.EVENTS:
            settings_store.put(db, notify.event_setting(event), bool(wanted))
    audit.record(db, "settings.notifications", actor=user.username, ip=ip)
    return read_notifications(db, user)


@router.post("/notifications/test")
async def test_notification(db: DbSession, user: CurrentUser, ip: ClientIp) -> dict[str, Any]:
    """Send one message to every enabled target and report what happened."""
    configured = [t for t in notify.targets(db) if t.enabled]
    if not configured:
        raise ApiError("no_targets", "No notification targets are configured.", status_code=400)

    delivered, problems = await notify.send_test(configured)
    audit.record(db, "settings.notify_test", actor=user.username, ip=ip)
    return {"targets": len(configured), "delivered": delivered, "problems": problems}
