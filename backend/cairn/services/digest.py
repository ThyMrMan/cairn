"""The periodic report: what happened, and what quietly did not.

An unattended archiver's characteristic failure is silence. A feed that stopped
answering, a profile whose cookies expired, a site whose captures have been
failing since a theme change — none of them raises an alarm at the moment it
breaks, and every one of them is discovered weeks later when somebody goes
looking for a post that was never archived. docs/13 puts it exactly right: this
is how you notice something broke three weeks ago.

So the digest is built around absence as much as activity. "Six captures
succeeded" is pleasant; "this site has not been captured in 34 days and its
feed has not returned an entry since the 3rd" is the sentence that pays for the
feature.

Two decisions worth stating:

**It is readable on demand, not only pushed.** A digest that exists only as a
notification is a digest nobody sees until they configure a webhook, and most
people never will. `build()` takes a window and has no side effects, so the
dashboard renders the same report the scheduler sends.

**Storage growth is measured against a stamp, not recomputed.** The total is
recorded each time a digest is sent, so growth is the difference between two
readings of the same number rather than a sum over captures — which would miss
everything retention deleted and everything that arrived by import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import AccessProfile, Capture, Feed, FeedItem, Job, Site
from cairn.db.types import to_iso, utcnow
from cairn.logging import get_logger
from cairn.services import settings_store

log = get_logger(__name__)

EVERY_DAYS_SETTING = "digest.every_days"
LAST_SENT_SETTING = "digest.last_sent"
LAST_TOTAL_SETTING = "digest.last_total_bytes"

DEFAULT_EVERY_DAYS = 7

# A site whose last capture is older than this, with nothing queued and no
# reason on record, is reported as quiet. Deliberately generous: a blog
# captured monthly on purpose should not be nagged about weekly.
QUIET_SITE_DAYS = 30
# Credentials expiring inside this window are worth mentioning in a weekly
# report; anything sooner already has its own notification.
EXPIRY_HORIZON_DAYS = 21

MAX_LISTED = 10


@dataclass(slots=True)
class Digest:
    """One period's report. Serializable, renderable, and side-effect free."""

    since: datetime
    until: datetime
    captures_ok: int = 0
    captures_partial: int = 0
    captures_failed: int = 0
    urls_archived: int = 0
    bytes_archived: int = 0
    new_items: int = 0
    failed_jobs: list[dict[str, Any]] = field(default_factory=list)
    quiet_sites: list[dict[str, Any]] = field(default_factory=list)
    stalled_feeds: list[dict[str, Any]] = field(default_factory=list)
    expiring_profiles: list[dict[str, Any]] = field(default_factory=list)
    # Sites whose *live* counterpart is gone or has moved. The other rows here
    # are about this archive failing; this one is about the web doing what the
    # archive exists for, and it is the most interesting line in the report.
    vanished_sites: list[dict[str, Any]] = field(default_factory=list)
    integrity: dict[str, Any] = field(default_factory=dict)
    total_bytes: int = 0
    growth_bytes: int | None = None
    sites: int = 0

    @property
    def days(self) -> float:
        return max((self.until - self.since).total_seconds() / 86400.0, 0.0)

    @property
    def has_problems(self) -> bool:
        return bool(
            self.captures_failed
            or self.failed_jobs
            or self.quiet_sites
            or self.stalled_feeds
            or self.expiring_profiles
            or self.vanished_sites
            or self.integrity.get("findings")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "since": to_iso(self.since),
            "until": to_iso(self.until),
            "days": round(self.days, 1),
            "sites": self.sites,
            "captures": {
                "ok": self.captures_ok,
                "partial": self.captures_partial,
                "failed": self.captures_failed,
            },
            "urls_archived": self.urls_archived,
            "bytes_archived": self.bytes_archived,
            "new_items": self.new_items,
            "failed_jobs": self.failed_jobs,
            "quiet_sites": self.quiet_sites,
            "stalled_feeds": self.stalled_feeds,
            "expiring_profiles": self.expiring_profiles,
            "vanished_sites": self.vanished_sites,
            "integrity": self.integrity,
            "total_bytes": self.total_bytes,
            "growth_bytes": self.growth_bytes,
            "has_problems": self.has_problems,
        }


def build(
    session: Session,
    settings: Settings,
    *,
    since: datetime,
    now: datetime | None = None,
    previous_total: int | None = None,
) -> Digest:
    """Everything that happened in a window, and everything that did not."""
    until = now or utcnow()
    digest = Digest(since=since, until=until)

    digest.sites = (
        session.scalar(select(func.count(Site.id)).where(Site.deleted_at.is_(None))) or 0
    )
    digest.total_bytes = (
        session.scalar(select(func.coalesce(func.sum(Site.size_bytes), 0)).where(
            Site.deleted_at.is_(None)
        ))
        or 0
    )
    if previous_total is not None:
        digest.growth_bytes = digest.total_bytes - previous_total

    _captures(session, digest)
    _new_items(session, digest)
    _failures(session, digest)
    _quiet_sites(session, digest)
    _stalled_feeds(session, digest)
    _expiring_profiles(session, digest)
    _vanished_sites(session, digest)
    _integrity(session, settings, digest)
    return digest


def _captures(session: Session, digest: Digest) -> None:
    rows = session.execute(
        select(Capture.status, func.count(Capture.id), func.sum(Capture.url_count),
               func.sum(Capture.bytes_written))
        .join(Site, Site.id == Capture.site_id)
        .where(
            Site.deleted_at.is_(None),
            Capture.started_at >= digest.since,
            Capture.started_at < digest.until,
        )
        .group_by(Capture.status)
    ).all()  # fmt: skip
    for status, count, urls, written in rows:
        if status == "ok":
            digest.captures_ok = count
        elif status == "partial":
            digest.captures_partial = count
        elif status in ("failed", "error"):
            digest.captures_failed = count
        digest.urls_archived += int(urls or 0)
        digest.bytes_archived += int(written or 0)


def _new_items(session: Session, digest: Digest) -> None:
    digest.new_items = (
        session.scalar(
            select(func.count(FeedItem.id)).where(
                FeedItem.first_seen_at >= digest.since,
                FeedItem.first_seen_at < digest.until,
            )
        )
        or 0
    )


def _failures(session: Session, digest: Digest) -> None:
    """Jobs that ended badly, named so the report can be acted on.

    A count alone sends somebody to the job list to find out what it was
    counting, which for a weekly summary is the whole of the work.
    """
    rows = session.execute(
        select(Job.id, Job.type, Job.error, Job.finished_at, Site.title)
        .outerjoin(Site, Site.id == Job.site_id)
        .where(
            Job.status.in_(("failed", "error")),
            Job.finished_at.isnot(None),
            Job.finished_at >= digest.since,
            Job.finished_at < digest.until,
        )
        .order_by(Job.finished_at.desc())
        .limit(MAX_LISTED)
    ).all()
    digest.failed_jobs = [
        {
            "job_id": row.id,
            "type": row.type,
            "site": row.title,
            "error": (row.error or "")[:200],
            "finished_at": to_iso(row.finished_at),
        }
        for row in rows
    ]


def _quiet_sites(session: Session, digest: Digest) -> None:
    """Sites nothing has archived in a long time.

    The point of the whole report. A site fails silently far more often than
    it fails loudly: the capture that stopped being scheduled, the feed that
    went quiet, the scope edit that excluded everything.
    """
    cutoff = digest.until - timedelta(days=QUIET_SITE_DAYS)
    rows = session.scalars(
        select(Site)
        .where(
            Site.deleted_at.is_(None),
            Site.last_capture_at.isnot(None),
            Site.last_capture_at < cutoff,
        )
        .order_by(Site.last_capture_at)
        .limit(MAX_LISTED)
    ).all()
    digest.quiet_sites = [
        {
            "site_id": site.id,
            "title": site.title,
            "last_capture_at": to_iso(site.last_capture_at) if site.last_capture_at else None,
            "days": int((digest.until - site.last_capture_at).days) if site.last_capture_at else 0,
        }
        for site in rows
    ]


def _stalled_feeds(session: Session, digest: Digest) -> None:
    """Feeds that are enabled, are being polled, and are returning nothing.

    Distinct from a feed that was turned off, which already has its own
    notification. This is the quieter case: the poll succeeds, the parse
    succeeds, and there simply are no entries — because the URL now serves a
    login page, or the blog moved.
    """
    cutoff = digest.until - timedelta(days=QUIET_SITE_DAYS)
    rows = session.execute(
        select(Feed.id, Feed.url, Feed.last_polled_at, Site.title, Site.id)
        .join(Site, Site.id == Feed.site_id)
        .where(
            Site.deleted_at.is_(None),
            Feed.enabled.is_(True),
            Feed.last_polled_at.isnot(None),
            Feed.last_polled_at >= cutoff,
        )
        .limit(200)
    ).all()

    stalled: list[dict[str, Any]] = []
    for row in rows:
        newest = session.scalar(
            select(func.max(FeedItem.first_seen_at)).where(FeedItem.feed_id == row.id)
        )
        if newest is not None and newest >= cutoff:
            continue
        stalled.append(
            {
                "feed_id": row.id,
                "url": row.url,
                "site_id": row[4],
                "site": row.title,
                "last_entry_at": to_iso(newest) if newest else None,
            }
        )
    digest.stalled_feeds = stalled[:MAX_LISTED]


def _expiring_profiles(session: Session, digest: Digest) -> None:
    horizon = digest.until + timedelta(days=EXPIRY_HORIZON_DAYS)
    rows = session.scalars(
        select(AccessProfile)
        .where(AccessProfile.expires_at.isnot(None), AccessProfile.expires_at <= horizon)
        .order_by(AccessProfile.expires_at)
        .limit(MAX_LISTED)
    ).all()
    digest.expiring_profiles = [
        {
            "profile_id": profile.id,
            "name": profile.name,
            "mode": profile.mode,
            "expires_at": to_iso(profile.expires_at) if profile.expires_at else None,
            "expired": bool(profile.expires_at and profile.expires_at <= digest.until),
        }
        for profile in rows
    ]


def _vanished_sites(session: Session, digest: Digest) -> None:
    """Sites whose live counterpart is gone or has moved.

    Only those two states. `unreachable` is as likely to be this end as
    theirs, and `blocked` says something about our user agent — putting either
    in a weekly report would train the reader to skim past the line that one
    day says a blog closed.
    """
    from cairn.services import sitehealth

    health = sitehealth.summary(session)
    digest.vanished_sites = [
        problem for problem in health["problems"] if problem["state"] in sitehealth.NOTABLE
    ][:MAX_LISTED]


def _integrity(session: Session, settings: Settings, digest: Digest) -> None:
    from cairn.services import integrity

    health = integrity.health(session, settings)
    last = health.get("last_run") or {}
    digest.integrity = {
        "captures": health.get("captures", 0),
        "verified": health.get("verified", 0),
        "oldest_unverified": health.get("oldest_unverified"),
        "last_run_at": last.get("finished_at"),
        "findings": len(last.get("findings") or []),
        "due": health.get("due", False),
    }


# ── rendering ────────────────────────────────────────────────────────────


def render_text(digest: Digest) -> str:
    """Plain text, because that is what every notification transport takes.

    Ordered problems-first. A weekly report read on a phone gets four lines of
    attention, and "everything is fine" is not what those four lines are for.
    """
    lines: list[str] = []
    days = round(digest.days) or 1

    if digest.failed_jobs:
        lines.append(f"{len(digest.failed_jobs)} job(s) failed:")
        lines += [
            f"  · {job['type']}"
            + (f" — {job['site']}" if job["site"] else "")
            + (f": {job['error']}" if job["error"] else "")
            for job in digest.failed_jobs[:5]
        ]
    if digest.quiet_sites:
        lines.append(f"{len(digest.quiet_sites)} site(s) not captured in over {QUIET_SITE_DAYS}d:")
        lines += [f"  · {s['title']} — {s['days']}d ago" for s in digest.quiet_sites[:5]]
    if digest.stalled_feeds:
        lines.append(f"{len(digest.stalled_feeds)} feed(s) polling but returning nothing:")
        lines += [f"  · {f['site']} — {f['url']}" for f in digest.stalled_feeds[:5]]
    if digest.vanished_sites:
        lines.append(f"{len(digest.vanished_sites)} archived site(s) are no longer as they were:")
        lines += [
            f"  · {s['title']} — {s['state']}"
            + (f" ({s['http_status']})" if s["http_status"] else "")
            for s in digest.vanished_sites[:5]
        ]
    if digest.expiring_profiles:
        lines.append("Access profiles needing attention:")
        lines += [
            f"  · {p['name']} — {'expired' if p['expired'] else 'expires'} {p['expires_at']}"
            for p in digest.expiring_profiles[:5]
        ]
    if digest.integrity.get("findings"):
        lines.append(f"Integrity: {digest.integrity['findings']} finding(s) at the last run.")

    if lines:
        lines.append("")

    lines.append(
        f"Last {days}d: {digest.captures_ok} capture(s) ok"
        + (f", {digest.captures_partial} partial" if digest.captures_partial else "")
        + (f", {digest.captures_failed} failed" if digest.captures_failed else "")
        + f"; {digest.urls_archived:,} URLs; {digest.new_items} new feed item(s)."
    )
    growth = ""
    if digest.growth_bytes is not None:
        sign = "+" if digest.growth_bytes >= 0 else "-"
        growth = f" ({sign}{_gb(abs(digest.growth_bytes))} this period)"
    lines.append(f"{digest.sites} site(s), {_gb(digest.total_bytes)} on disk{growth}.")

    integrity = digest.integrity
    if integrity.get("captures"):
        lines.append(
            f"Integrity: {integrity['verified']}/{integrity['captures']} captures verified"
            + (f", oldest unverified {integrity['oldest_unverified']['site_title']}"
               if integrity.get("oldest_unverified") else "")
            + "."
        )  # fmt: skip
    if not digest.has_problems:
        lines.append("Nothing needs attention.")
    return "\n".join(lines)


def _gb(value: int) -> str:
    if value >= 1024**3:
        return f"{value / 1024**3:.1f} GB"
    if value >= 1024**2:
        return f"{value / 1024**2:.0f} MB"
    return f"{value / 1024:.0f} KB"


# ── the schedule ─────────────────────────────────────────────────────────


def every_days(session: Session) -> int:
    return settings_store.get_int(session, EVERY_DAYS_SETTING, DEFAULT_EVERY_DAYS)


def window_start(session: Session, *, now: datetime) -> datetime:
    """When the last digest went out, or one period ago if none ever has."""
    raw = str(settings_store.get(session, LAST_SENT_SETTING, "") or "")
    if raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:  # pragma: no cover — hand-edited setting
            pass
    return now - timedelta(days=max(every_days(session), 1))


def is_due(session: Session, *, now: datetime) -> bool:
    days = every_days(session)
    if days <= 0:
        return False
    raw = str(settings_store.get(session, LAST_SENT_SETTING, "") or "")
    if not raw:
        # Never sent. Wait a full period rather than reporting on an empty
        # first week — a digest that arrives an hour after installation says
        # nothing and teaches the reader to ignore the next one.
        settings_store.put(session, LAST_SENT_SETTING, now.isoformat())
        return False
    try:
        last = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:  # pragma: no cover — hand-edited setting
        return True
    return now - last >= timedelta(days=days)


def mark_sent(session: Session, digest: Digest, *, now: datetime) -> None:
    settings_store.put(session, LAST_SENT_SETTING, now.isoformat())
    settings_store.put(session, LAST_TOTAL_SETTING, str(digest.total_bytes))


def previous_total(session: Session) -> int | None:
    raw = str(settings_store.get(session, LAST_TOTAL_SETTING, "") or "")
    try:
        return int(raw)
    except ValueError:
        return None
