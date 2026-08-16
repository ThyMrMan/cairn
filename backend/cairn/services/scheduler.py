"""The scheduler: one ticker, and a database query that answers "what now?".

docs/08 specified APScheduler with a SQLAlchemy job store. This is not that,
and the reason is worth stating because the alternative looks obviously
correct until it is measured.

**A persistent job store is a second copy of the schedule.** `feeds` already
holds `interval_min`, `enabled` and `next_poll_at`; a job store holds the fire
time too, so every interval change is two writes that can disagree, and the
disagreement is invisible until a feed silently stops polling.

**Its two failure modes are both defaults, and both are silent.** Measured
against APScheduler 3.11 with a SQLAlchemy store on SQLite, restarting the
process after a fire time had passed:

  - default `misfire_grace_time=1`: the run is **dropped**, with a log line and
    nothing else. A container restarted across a feed's poll time simply
    misses that poll — six hours of latency on a six-hour feed.
  - `misfire_grace_time=None` with `coalesce=False`: the whole backlog fires at
    once. A 30-second outage of a 3-second job produced **12 simultaneous
    runs**. Scaled up, a week of downtime is 28 concurrent polls of one feed —
    the precise thing the jitter requirement exists to prevent.

Configured correctly (`misfire_grace_time=None, coalesce=True`) it behaves; it
is two non-default settings away from either failure, in a component nobody
looks at until it is already wrong.

A due-time query has neither problem by construction. Nothing persists a fire
time that could be missed, so a container down for a week comes back and polls
each overdue feed exactly once. What it costs is cron expressions, which
nothing here needs: every built-in schedule in docs/08 is an interval, and
quiet hours are a gate on running rather than a time to run at.

**Polls are sequential and capped per tick.** Twenty polls a minute is far more
than a single-user instance can want, and doing them one at a time is politeness
that needs no coordination — no two requests to one host can overlap because no
two requests overlap at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cairn.config import Settings
from cairn.crypto.sealing import Sealer
from cairn.db.models import Feed, FeedItem, Job, Site
from cairn.db.types import utcnow
from cairn.logging import get_logger
from cairn.services import feeds as feed_service
from cairn.services import notify, settings_store

log = get_logger(__name__)

TICK_SECONDS = 60
MAX_POLLS_PER_TICK = 20
# Seeds per feed capture. Ten new posts is one job with ten seeds, not ten
# jobs; a site that published 400 overnight is split rather than handed to
# wget as one enormous command (docs/08).
MAX_SEEDS_PER_JOB = 50

QUIET_HOURS_SETTING = "jobs.quiet_hours"
RECAPTURE_SETTING = "schedule.full_recapture_days"
TRASH_PURGE_SETTING = "schedule.last_trash_purge"
ROLLUP_SETTING = "schedule.last_stats_rollup"
DISK_WARNED_SETTING = "schedule.last_disk_warning"
VERIFY_SETTING = "schedule.last_integrity_verify"
RETENTION_SETTING = "schedule.last_retention"

TRASH_PURGE_INTERVAL = timedelta(days=1)
ROLLUP_INTERVAL = timedelta(hours=1)
DISK_WARN_INTERVAL = timedelta(days=1)
RETENTION_INTERVAL = timedelta(days=1)


@dataclass(slots=True)
class TickReport:
    """What one pass did. Returned so a test can assert on it directly."""

    polled: int = 0
    new_items: int = 0
    jobs: list[int] = field(default_factory=list)
    deferred: int = 0
    maintenance: list[str] = field(default_factory=list)


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        supervisor: Any,
        sealer: Sealer,
    ) -> None:
        self._settings = settings
        self._sessions = session_factory
        self._supervisor = supervisor
        self._sealer = sealer
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="cairn-scheduler")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        """Sleep first, then tick.

        A boot is the worst moment to start fetching: migrations have just run,
        the supervisor is recovering interrupted jobs, and anything overdue has
        been overdue for a while already and can wait another minute. It also
        means a test that starts the app does not get a poll it did not ask
        for — `tick()` is called directly where a test wants one.
        """
        while not self._stopping:
            await asyncio.sleep(TICK_SECONDS)
            try:
                await self.tick()
            except Exception:  # pragma: no cover — a bad tick must not end the loop
                log.exception("scheduler tick failed")

    # ── one pass ─────────────────────────────────────────────────────────

    async def tick(self, *, now: datetime | None = None) -> TickReport:
        now = now or utcnow()
        report = TickReport()

        due = await asyncio.to_thread(self._due_feed_ids, now)
        for feed_id in due[:MAX_POLLS_PER_TICK]:
            outcome = await self.poll(feed_id, now=now)
            if outcome is None:
                continue
            report.polled += 1
            report.new_items += len(outcome.new_items)

        job_ids, deferred = await asyncio.to_thread(self._dispatch_pending, now)
        if job_ids:
            self._supervisor.notify()
        report.jobs = job_ids
        report.deferred = deferred
        report.maintenance = await asyncio.to_thread(self._maintenance, now)

        checked = await self._check_health(now)
        if checked:
            report.maintenance.append(f"checked {checked} site(s) for signs of life")

        summary = await asyncio.to_thread(self._due_digest, now)
        if summary is not None:
            title, body = summary
            await notify.send(self._sessions, notify.DIGEST, title=title, body=body)
            report.maintenance.append("sent the periodic digest")

        low = await asyncio.to_thread(self._disk_shortfall, now)
        if low is not None:
            free, floor = low
            await notify.send(
                self._sessions,
                notify.DISK_LOW,
                title="Free disk space is below the floor",
                body=f"{free // 1024**3} GB free, floor is {floor // 1024**3} GB.",
                priority="high",
            )
        return report

    async def _check_health(self, now: datetime) -> int:
        """Ask a few archived sites whether they still exist.

        A handful per tick rather than all of them on a timer: these are other
        people's servers, one request each is enough, and spreading the sweep
        across ticks means it is never a burst. The interval is per site — a
        site checked yesterday is not due — so the work finds its own level.
        """
        from cairn.services import sitehealth

        due = await asyncio.to_thread(self._due_health, now)
        if not due:
            return 0

        from cairn.discovery.fetch import Fetcher

        announced: list[tuple[str, str]] = []
        async with Fetcher(user_agent=_default_user_agent()) as fetcher:
            for site_id, seed in due:
                found = await sitehealth.probe(fetcher, seed)
                changed = await asyncio.to_thread(self._record_health, site_id, found, now)
                if changed:
                    announced.append((seed, changed))

        for seed, state in announced:
            if state not in sitehealth.NOTABLE:
                continue
            await notify.send(
                self._sessions,
                notify.SITE_GONE,
                title=(
                    "An archived site has moved"
                    if state == sitehealth.MOVED
                    else "An archived site is gone"
                ),
                body=f"{seed}\n{sitehealth.describe(state)}",
            )
        return len(due)

    def _due_health(self, now: datetime) -> list[tuple[int, str]]:
        from cairn.services import sitehealth

        with self._sessions() as session:
            days = settings_store.get_int(
                session, sitehealth.EVERY_DAYS_SETTING, sitehealth.DEFAULT_EVERY_DAYS
            )
            if days <= 0:
                return []
            return [(s.id, s.seed_url) for s in sitehealth.due_sites(session, now=now, days=days)]

    def _record_health(self, site_id: int, probe: Any, now: datetime) -> str | None:
        from cairn.services import sitehealth

        with self._sessions() as session:
            site = session.get(Site, site_id)
            if site is None:  # pragma: no cover — deleted mid-check
                return None
            changed = sitehealth.record(session, site, probe, now=now)
            session.commit()
            return changed

    def _due_digest(self, now: datetime) -> tuple[str, str] | None:
        """Build and stamp the periodic report, or nothing if it is not due.

        Stamped here rather than after delivery, and deliberately: a target
        that is down would otherwise make every tick rebuild and re-send the
        report for the rest of the outage. A digest is a summary of a window —
        missing one is a gap in the reporting, not a gap in the archive, and
        the same information is on the dashboard either way.
        """
        from cairn.services import digest as digest_service

        with self._sessions() as session:
            if not digest_service.is_due(session, now=now):
                session.commit()
                return None
            report = digest_service.build(
                session,
                self._settings,
                since=digest_service.window_start(session, now=now),
                now=now,
                previous_total=digest_service.previous_total(session),
            )
            digest_service.mark_sent(session, report, now=now)
            session.commit()

        days = round(report.days) or 1
        headline = (
            f"Cairn: {len(report.failed_jobs) + len(report.quiet_sites)} thing(s) need attention"
            if report.has_problems
            else f"Cairn: {days} quiet day(s)"
        )
        return headline, digest_service.render_text(report)

    def _disk_shortfall(self, now: datetime) -> tuple[int, int] | None:
        """Free space against the floor, at most once a day.

        The condition persists — a full disk stays full — so an unthrottled
        check would push once a minute until somebody freed space, which is how
        a person learns to mute the channel that was going to tell them
        something important later.
        """
        with self._sessions() as session:
            floor = settings_store.get_int(session, "storage.free_space_floor_bytes", 0)
            if floor <= 0:
                return None
            try:
                free = shutil.disk_usage(self._settings.data_dir).free
            except OSError:  # pragma: no cover — the volume went away
                return None
            if free >= floor:
                # Recovered: forget the stamp so the next shortfall is reported
                # immediately rather than up to a day late.
                settings_store.put(session, DISK_WARNED_SETTING, "")
                session.commit()
                return None
            if not _elapsed(session, DISK_WARNED_SETTING, DISK_WARN_INTERVAL, now):
                return None
            settings_store.put(session, DISK_WARNED_SETTING, now.isoformat())
            session.commit()
            return free, floor

    def _due_feed_ids(self, now: datetime) -> list[int]:
        with self._sessions() as session:
            return [feed.id for feed in feed_service.due_feeds(session, now=now)]

    # ── polling one feed ─────────────────────────────────────────────────

    async def poll(
        self, feed_id: int, *, now: datetime | None = None
    ) -> feed_service.PollOutcome | None:
        """Fetch a feed and record what it found.

        Also the implementation of "Poll now" in the UI, so nothing about it
        may assume it was reached from the ticker.
        """
        prepared = await asyncio.to_thread(self._prepare_poll, feed_id)
        if prepared is None:
            return None
        state, auth, temp_dir = prepared

        try:
            from cairn.discovery.fetch import Fetcher

            async with Fetcher(
                user_agent=auth.get("user_agent") or _default_user_agent(),
                cookies_file=auth.get("cookies_file"),
            ) as fetcher:
                result = await feed_service.fetch(state, fetcher=fetcher)
        finally:
            # Plaintext cookies, removed the moment the poll ends rather than
            # left for the next boot sweep (docs/06).
            if temp_dir is not None:
                await asyncio.to_thread(_remove_tree, temp_dir)

        outcome = await asyncio.to_thread(self._apply_poll, feed_id, result)
        if outcome is not None and outcome.disabled:
            await notify.send(
                self._sessions,
                notify.FEED_DISABLED,
                title="A feed was turned off",
                body=f"{state.url}\n{outcome.error or ''}".strip(),
            )
        if outcome is not None and outcome.gone_items:
            await self._announce_disappearances(feed_id, outcome.gone_items)
        return outcome

    def _prepare_poll(self, feed_id: int) -> tuple[Any, dict[str, Any], Path | None] | None:
        """Snapshot the row and materialize any credentials the fetch needs.

        The profile is materialized for the poll as well as the capture. A
        gated site can gate its feed too, and a poll that reads the
        interstitial parses as "no entries" — which is indistinguishable from a
        blog that has not posted, forever.
        """
        from cairn.services import profiles

        with self._sessions() as session:
            feed = session.get(Feed, feed_id)
            if feed is None:
                return None
            site = session.get(Site, feed.site_id)
            if site is None or site.deleted_at is not None:
                return None

            state = feed_service.FeedState(
                id=feed.id,
                url=feed.url,
                kind=feed.kind,
                etag=feed.etag,
                last_modified=feed.last_modified,
            )
            auth: dict[str, Any] = {}
            temp_dir: Path | None = None
            if site.profile_id is not None:
                temp_dir = self._settings.tmp_dir / f"feed-{feed.id}"
                temp_dir.mkdir(parents=True, exist_ok=True)
                with contextlib.suppress(OSError):
                    temp_dir.chmod(0o700)
                material = profiles.materialize(
                    session,
                    self._sealer,
                    site.profile_id,
                    temp_dir,
                    # Passed so a browser-profile-only site can have a jar
                    # derived for it. A feed poll is a plain HTTP fetch and has
                    # no way to use a tarball, so before this a gated blog with
                    # a browser profile polled its feed signed out.
                    self._settings,
                    hosts=[site.primary_host] if site.primary_host else None,
                )
                if material is not None:
                    # Guarded: `str(None)` wrote the literal "None" as a path,
                    # which is a file that does not exist and an error nobody
                    # would recognise.
                    if material.cookies_file is not None:
                        auth["cookies_file"] = str(material.cookies_file)
                    if material.user_agent:
                        auth["user_agent"] = material.user_agent
            return state, auth, temp_dir

    def _apply_poll(self, feed_id: int, result: Any) -> feed_service.PollOutcome | None:
        with self._sessions() as session:
            feed = session.get(Feed, feed_id)
            if feed is None:  # pragma: no cover — deleted mid-poll
                return None
            outcome = feed_service.apply(session, feed, result)
            session.commit()
            log.info(
                "feed polled",
                extra={
                    "feed": feed_id,
                    "status": outcome.status,
                    "action": outcome.action,
                    "new": len(outcome.new_items),
                },
            )
            return outcome

    async def _announce_disappearances(self, feed_id: int, item_ids: list[int]) -> None:
        """The notification that says a post you archived is gone upstream.

        The one worth having: it is the moment the archive paid for itself, and
        it is a reason to protect that capture from any retention policy.
        """
        urls = await asyncio.to_thread(self._urls_of, item_ids)
        await notify.send(
            self._sessions,
            notify.URLS_DISAPPEARED,
            title=f"{len(item_ids)} archived URL(s) no longer exist upstream",
            body="\n".join(urls[:10]),
        )

    def _urls_of(self, item_ids: list[int]) -> list[str]:
        with self._sessions() as session:
            return list(
                session.scalars(select(FeedItem.url).where(FeedItem.id.in_(item_ids))).all()
            )

    # ── turning pending items into captures ──────────────────────────────

    def _dispatch_pending(self, now: datetime) -> tuple[list[int], int]:
        """Enqueue a capture for every feed holding pending items.

        Returns the job ids and how many feeds were held back by quiet hours.
        Deferral is not a failure and not a loss: the items stay pending and
        the next tick inside the window picks them up.
        """
        job_ids: list[int] = []
        deferred = 0
        with self._sessions() as session:
            if in_quiet_hours(session, now):
                held = session.scalars(
                    select(Feed.id)
                    .join(FeedItem, FeedItem.feed_id == Feed.id)
                    .where(
                        Feed.enabled.is_(True),
                        Feed.auto_capture.is_(True),
                        FeedItem.status == "pending",
                    )
                    .distinct()
                ).all()
                return [], len(held)

            feeds = session.scalars(
                select(Feed)
                .join(FeedItem, FeedItem.feed_id == Feed.id)
                .where(
                    Feed.enabled.is_(True),
                    Feed.auto_capture.is_(True),
                    FeedItem.status == "pending",
                    # A feed whose captures keep failing waits. Without this
                    # the retry cadence is this tick — one attempt a minute at
                    # somebody else's server, forever.
                    (Feed.next_capture_at.is_(None)) | (Feed.next_capture_at <= now),
                )
                .distinct()
            ).all()
            for feed in feeds:
                job_ids += self._capture_feed(session, feed)
            session.commit()
        return job_ids, deferred

    def capture_pending(self, session: Session, feed: Feed) -> list[int]:
        """Capture a feed's pending items now, regardless of the schedule.

        The "Capture now" button. Quiet hours are a rule about unattended work,
        so pressing a button is never subject to them.
        """
        job_ids = self._capture_feed(session, feed)
        session.commit()
        if job_ids:
            self._supervisor.notify()
        return job_ids

    def _capture_feed(self, session: Session, feed: Feed) -> list[int]:
        from cairn.services import sites as site_service

        site = session.get(Site, feed.site_id)
        if site is None or site.deleted_at is not None:
            return []

        # Every other path that enqueues checks this first — `_due_recaptures`,
        # `_due_verification`, `_due_retention` and POST /sites/{id}/capture,
        # which answers 409. This one did not, and it is the only one called
        # once a tick over every feed holding a pending item.
        #
        # An item leaves `pending` only when a capture *succeeds*; a failed one
        # returns it. So the missing check was not a duplicate job now and then
        # — it was one job every sixty seconds for as long as the capture kept
        # failing. Found on a running instance at 105 queued for one site,
        # climbing at exactly a job a minute.
        #
        # Per site rather than per feed, matching the capture endpoint: two
        # feeds on one site would serialize behind each other anyway, and the
        # second is picked up on the next tick after the first finishes.
        busy = session.scalar(
            select(Job.id)
            .where(
                Job.type == "capture",
                Job.site_id == site.id,
                Job.status.in_(("queued", "running")),
            )
            .limit(1)
        )
        if busy is not None:
            return []

        items = feed_service.pending_items(session, feed.id)
        if not items:
            return []

        # A seed the scope would refuse is not a capture that will fail — it is
        # a capture that will quietly archive nothing. Splitting here means the
        # poll history can say so instead.
        scope = site_service.resolved_scope(session, site)
        inside, outside = feed_service.split_by_scope([item.url for item in items], scope)
        allowed = set(inside)
        for item in items:
            if item.url not in allowed:
                item.status = "skipped"
        if outside:
            log.warning(
                "feed items fall outside the site's scope and were not captured",
                extra={"feed": feed.id, "count": len(outside), "example": outside[0]},
            )
        wanted = [item for item in items if item.url in allowed]
        if not wanted:
            return []

        job_ids: list[int] = []
        for batch in _chunks(wanted, MAX_SEEDS_PER_JOB):
            job = self._supervisor.enqueue(
                session,
                job_type="capture",
                site_id=site.id,
                spec={
                    "kind": "feed",
                    "feed_id": feed.id,
                    "item_ids": [item.id for item in batch],
                    "extra_seeds": [item.url for item in batch],
                    # The whole point of an incremental capture: this run is
                    # about these posts, not about re-enumerating the site.
                    "only_extra_seeds": True,
                    "max_depth": 1,
                },
                # Behind a user-initiated capture. A scheduled poll must never
                # make somebody wait for something they asked for.
                priority=200,
            )
            job_ids.append(job.id)
        return job_ids

    # ── periodic housekeeping ────────────────────────────────────────────

    def _maintenance(self, now: datetime) -> list[str]:
        done: list[str] = []
        with self._sessions() as session:
            if _elapsed(session, TRASH_PURGE_SETTING, TRASH_PURGE_INTERVAL, now):
                from cairn.services import trash

                purged, freed = trash.purge_expired(session, self._settings)
                settings_store.put(session, TRASH_PURGE_SETTING, now.isoformat())
                if purged:
                    done.append(f"purged {purged} trashed site(s), {freed} bytes")
                session.commit()

            if _elapsed(session, ROLLUP_SETTING, ROLLUP_INTERVAL, now):
                changed = self._roll_up_sizes(session)
                settings_store.put(session, ROLLUP_SETTING, now.isoformat())
                if changed:
                    done.append(f"recomputed size for {changed} site(s)")
                session.commit()

            done += self._due_recaptures(session, now)
            done += self._due_verification(session, now)
            done += self._due_retention(session, now)
            session.commit()
        return done

    def _due_retention(self, session: Session, now: datetime) -> list[str]:
        """Queue a retention pass daily, for sites whose policy is on.

        The stamp is written whether or not anything was queued, so a hundred
        sites with retention off do not cost a full plan computation every
        minute.
        """
        from cairn.services import retention

        if not _elapsed(session, RETENTION_SETTING, RETENTION_INTERVAL, now):
            return []
        settings_store.put(session, RETENTION_SETTING, now.isoformat())

        pending = session.scalar(
            select(Job.id).where(Job.type == "purge", Job.status.in_(("queued", "running")))
        )
        if pending:
            return []
        sites = retention.due_sites(session, self._settings)
        if not sites:
            return []
        job = self._supervisor.enqueue(
            session, job_type="purge", site_id=None, spec={}, priority=250
        )
        return [f"queued retention for {len(sites)} site(s) as job {job.id}"]

    def _due_verification(self, session: Session, now: datetime) -> list[str]:
        """Queue the integrity pass when the interval has elapsed.

        Enqueued rather than run here: it reads every archived byte, so it
        belongs in the same queue as the captures it competes with for the
        array, and it has to be cancellable.

        The stamp is written when the job is *queued*, not when it finishes,
        for the same reason the other maintenance stamps are: a pass that dies
        halfway must not be re-queued on the next tick, sixty seconds later,
        forever.
        """
        days = settings_store.get_int(session, "integrity.verify_days", 7)
        if days <= 0:
            return []
        if not _elapsed(session, VERIFY_SETTING, timedelta(days=days), now):
            return []
        pending = session.scalar(
            select(Job.id).where(Job.type == "verify", Job.status.in_(("queued", "running")))
        )
        settings_store.put(session, VERIFY_SETTING, now.isoformat())
        if pending:
            return []
        job = self._supervisor.enqueue(
            session, job_type="verify", site_id=None, spec={"deep": False}, priority=250
        )
        return [f"queued integrity verification as job {job.id}"]

    def _roll_up_sizes(self, session: Session) -> int:
        """Recompute what each site occupies on disk.

        The stored size is written after a capture, so it drifts whenever
        anything else touches the tree: a capture deleted, a WARC removed by
        hand over the share, a restore from backup.
        """
        from cairn.services import storage

        changed = 0
        for site in session.scalars(select(Site).where(Site.deleted_at.is_(None))).all():
            directory = storage.site_dir(self._settings, site.archive_path)
            if not directory.exists():
                continue
            actual = storage.directory_size(directory)
            if actual != site.size_bytes:
                site.size_bytes = actual
                changed += 1
        return changed

    def _due_recaptures(self, session: Session, now: datetime) -> list[str]:
        """Full recaptures, for sites that asked for one.

        Off unless a number of days is set, and set per site. docs/08 is right
        that this is the setting most likely to be switched on without thinking
        and then quietly consume terabytes, which is why the UI shows the
        estimate before it can be enabled.
        """
        days = settings_store.get_int(session, RECAPTURE_SETTING, 0)
        if days <= 0 or in_quiet_hours(session, now):
            return []

        cutoff = now - timedelta(days=days)
        stale = session.scalars(
            select(Site).where(
                Site.deleted_at.is_(None),
                Site.status.in_(("ready", "indexed")),
                Site.last_capture_at.isnot(None),
                Site.last_capture_at <= cutoff,
            )
        ).all()
        done: list[str] = []
        for site in stale:
            busy = session.scalar(
                select(Job.id)
                .where(Job.site_id == site.id, Job.status.in_(("queued", "running")))
                .limit(1)
            )
            if busy is not None:
                continue
            job = self._supervisor.enqueue(
                session,
                job_type="capture",
                site_id=site.id,
                spec={"kind": "full", "scheduled": True},
                priority=300,
            )
            done.append(f"queued a full recapture of {site.slug} (job {job.id})")
        return done


# ── quiet hours ──────────────────────────────────────────────────────────


def in_quiet_hours(session: Session, now: datetime | None = None) -> bool:
    """Whether unattended capture work is currently held back.

    Off by default, which is a departure from docs/08's "capture only
    01:00-07:00". That default would mean adding a feed, watching a post
    appear, and seeing nothing happen for eighteen hours with no explanation —
    and the only thing it would be throttling is an incremental capture of a
    few hundred kilobytes, because full recapture is off by default too. The
    window exists and is preloaded with those hours; switching it on is a
    decision somebody makes about their own bandwidth.

    Local time deliberately: "don't crawl during the evening" is a statement
    about the household, not about UTC. The container's TZ is what decides.
    """
    raw: Any = settings_store.get(session, QUIET_HOURS_SETTING, {})
    if not isinstance(raw, dict) or not raw.get("enabled"):
        return False
    start = _parse_clock(raw.get("start"), clock_time(1, 0))
    end = _parse_clock(raw.get("end"), clock_time(7, 0))
    # The setting names the window in which work MAY run, so quiet means
    # outside it. A window that wraps midnight is the normal case.
    current = (now or utcnow()).astimezone().time()
    inside = start <= current < end if start <= end else (current >= start or current < end)
    return not inside


def _parse_clock(value: Any, fallback: clock_time) -> clock_time:
    try:
        hour, minute = str(value).split(":", 1)
        return clock_time(int(hour) % 24, int(minute) % 60)
    except (AttributeError, TypeError, ValueError):
        return fallback


# ── helpers ──────────────────────────────────────────────────────────────


def _elapsed(session: Session, key: str, interval: timedelta, now: datetime) -> bool:
    raw = settings_store.get(session, key, "")
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(str(raw))
    except ValueError:  # pragma: no cover — hand-edited setting
        return True
    if last.tzinfo is None:  # pragma: no cover — hand-edited setting
        return True
    return (now - last) >= interval


def _chunks[T](values: list[T], size: int) -> list[list[T]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def _default_user_agent() -> str:
    from cairn.discovery.fetch import USER_AGENT

    return USER_AGENT


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
