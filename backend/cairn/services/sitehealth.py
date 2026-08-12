"""Is the live site still there?

The question the archive exists to answer, asked the other way round. Knowing
that three of the blogs you archived now return 404 is interesting on its own,
and it is the strongest argument the tool ever makes for itself: those pages
are now only in your copy.

Four decisions, and each of them is about not crying wolf.

**One bad check is not a dead site.** A blog is briefly 502, a container is
briefly without DNS, a NAS reboots mid-check. A state change is therefore
believed only after `CONFIRMATIONS` checks agree, and until then the reported
state is the previous one. `since` records when the *believed* state began, so
the answer is "gone since March" rather than "gone since the last tick".

**Unreachable is not gone.** A DNS failure or a refused connection says
something about the path between here and there, and the honest report is "we
could not reach it", not "it is gone". These are different words in the UI and
different rows in the digest, because the actions they call for are different:
one is "the blog closed", the other is "check your network".

**A redirect off the registrable domain is a move, not a death.** A blog that
now 301s to a custom domain is alive, has changed address, and wants a second
seed — which is exactly what multi-seed sites are for, so the report says so.

**403 and 429 are not answers about existence.** A site behind Cloudflare, or
one rate-limiting an unfamiliar user agent, is telling us about us. That is
`blocked`, kept distinct from `gone` so nobody deletes an archive over it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.db.models import Site, SiteHealth
from cairn.db.types import to_iso, utcnow
from cairn.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover — typing only
    from cairn.discovery.fetch import Fetcher

log = get_logger(__name__)

LIVE = "live"
GONE = "gone"
MOVED = "moved"
UNREACHABLE = "unreachable"
BLOCKED = "blocked"

# States worth telling somebody about. `moved` is in here because it is
# actionable — add the new address as a seed — and `unreachable` is not,
# because the most likely cause is at this end.
NOTABLE = (GONE, MOVED)

EVERY_DAYS_SETTING = "health.every_days"
LAST_RUN_SETTING = "schedule.last_health_check"
DEFAULT_EVERY_DAYS = 7

# How many agreeing checks it takes to change a site's reported state.
CONFIRMATIONS = 2
# Sites checked per tick. One request each, spaced a minute apart by the ticker
# itself — this is somebody else's server and there is no hurry.
MAX_PER_TICK = 10


@dataclass(slots=True)
class Probe:
    """What one request found, before any judgement about persistence."""

    state: str
    http_status: int | None = None
    final_url: str | None = None
    error: str | None = None


async def probe(fetcher: Fetcher, seed_url: str) -> Probe:
    """One request at the seed, classified.

    Takes the URL rather than the site so it needs no open session: the
    scheduler probes off the event loop with the database closed, and passing
    a detached ORM object across that boundary is how a lazy load turns into a
    DetachedInstanceError at three in the morning.

    `head_or_get` rather than a plain HEAD: plenty of servers answer HEAD with
    405 or a lie and serve the page perfectly well on GET, and reporting a
    living blog as gone because of that would be the worst possible bug in
    this feature.
    """
    from cairn.discovery import hosts as host_classify

    result = await fetcher.head_or_get(seed_url)
    if result.error:
        return Probe(state=UNREACHABLE, error=result.error[:300])

    status = result.status
    if status in (401, 403, 429) or status == 451:
        return Probe(state=BLOCKED, http_status=status, final_url=str(result.url))
    if status in (404, 410):
        return Probe(state=GONE, http_status=status, final_url=str(result.url))
    if status >= 500:
        # A server error is the site failing, not the site ending. Treated as
        # unreachable so it needs confirmation and never reads as "gone".
        return Probe(state=UNREACHABLE, http_status=status, error=f"HTTP {status}")
    if status >= 400:
        return Probe(state=UNREACHABLE, http_status=status, error=f"HTTP {status}")

    # The fetcher follows redirects, so a move shows up as a final URL on a
    # different registrable domain. Compared at the registrable level: a blog
    # moving from `example.com` to `www.example.com` has not moved.
    final = str(result.url or seed_url)
    here = host_classify.registrable_domain(host_classify.host_of(seed_url))
    there = host_classify.registrable_domain(host_classify.host_of(final))
    if there and here and there != here:
        return Probe(state=MOVED, http_status=status, final_url=final)
    return Probe(state=LIVE, http_status=status, final_url=final)


def record(
    session: Session, site: Site, found: Probe, *, now: datetime | None = None
) -> str | None:
    """Fold one probe into a site's health. Returns the new state if it changed.

    The confirmation counter lives here rather than in the caller so that a
    manual "check now" from the UI and the scheduled sweep agree about what
    counts as a change — otherwise pressing the button twice could announce a
    site as gone that the sweep would still be waiting on.
    """
    now = now or utcnow()
    row = session.get(SiteHealth, site.id)
    if row is None:
        row = SiteHealth(site_id=site.id, state=found.state, since=now, checked_at=now)
        session.add(row)
        _apply(row, found, now)
        session.flush()
        # The first check establishes the baseline. Announcing "gone" for a
        # site that was already gone when it was added would be true and
        # useless — it was archived precisely because it was disappearing.
        return None

    _apply(row, found, now)
    if found.state == row.state:
        row.pending_state = None
        row.consecutive = 0
        session.flush()
        return None

    if row.pending_state == found.state:
        row.consecutive += 1
    else:
        row.pending_state = found.state
        row.consecutive = 1

    changed: str | None = None
    if row.consecutive >= CONFIRMATIONS:
        row.state = found.state
        row.since = now
        row.pending_state = None
        row.consecutive = 0
        changed = found.state
    session.flush()
    return changed


def _apply(row: SiteHealth, found: Probe, now: datetime) -> None:
    row.checked_at = now
    row.http_status = found.http_status
    row.final_url = found.final_url
    row.error = found.error


def due_sites(
    session: Session,
    *,
    now: datetime | None = None,
    days: int = DEFAULT_EVERY_DAYS,
    limit: int = MAX_PER_TICK,
) -> list[Site]:
    """Sites whose last check is older than the interval, never-checked first.

    A left join rather than two queries so a site with no health row sorts
    first without needing a sentinel date — being unchecked is more urgent
    than being checked a week ago.
    """
    now = now or utcnow()
    cutoff = now - timedelta(days=max(days, 1))
    rows = session.execute(
        select(Site, SiteHealth.checked_at)
        .outerjoin(SiteHealth, SiteHealth.site_id == Site.id)
        .where(Site.deleted_at.is_(None))
        .order_by(SiteHealth.checked_at.is_(None).desc(), SiteHealth.checked_at)
        .limit(limit * 4)
    ).all()
    return [site for site, checked in rows if checked is None or checked < cutoff][:limit]


def summary(session: Session) -> dict[str, Any]:
    """Counts by state, and the sites that are not live."""
    rows = session.execute(
        select(SiteHealth, Site.title)
        .join(Site, Site.id == SiteHealth.site_id)
        .where(Site.deleted_at.is_(None))
    ).all()

    counts: dict[str, int] = {}
    problems: list[dict[str, Any]] = []
    for row, title in rows:
        counts[row.state] = counts.get(row.state, 0) + 1
        if row.state == LIVE:
            continue
        problems.append(
            {
                "site_id": row.site_id,
                "title": title,
                "state": row.state,
                "http_status": row.http_status,
                "final_url": row.final_url,
                "error": row.error,
                "since": to_iso(row.since),
                "checked_at": to_iso(row.checked_at),
            }
        )
    problems.sort(key=lambda p: (p["state"] != GONE, p["since"] or ""))
    return {"counts": counts, "problems": problems, "checked": len(rows)}


def for_site(session: Session, site_id: int) -> dict[str, Any] | None:
    row = session.get(SiteHealth, site_id)
    if row is None:
        return None
    return {
        "state": row.state,
        "http_status": row.http_status,
        "final_url": row.final_url,
        "error": row.error,
        "since": to_iso(row.since),
        "checked_at": to_iso(row.checked_at),
        "pending_state": row.pending_state,
        "consecutive": row.consecutive,
    }


def describe(state: str, *, status: int | None = None, final_url: str | None = None) -> str:
    """One sentence, written to be shown to a person."""
    if state == GONE:
        return f"The live site returns {status or 404}. These pages are now only in your archive."
    if state == MOVED:
        return (
            f"The live site now redirects to {final_url}. Add it as a second seed "
            "to keep archiving it."
        )
    if state == BLOCKED:
        return (
            f"The live site answered {status}. That is about us, not about whether it "
            "exists — an access profile may be needed."
        )
    if state == UNREACHABLE:
        return "The live site could not be reached. That is as likely to be this end as theirs."
    return "The live site answers normally."
