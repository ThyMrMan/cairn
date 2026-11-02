"""The site-facing half of the Wayback pre-fill (docs/17, docs/19).

Per-site settings live in `sites.scope_settings`, like the media policy:

- `url_groups`: URL shapes that serve one item (decision 1 in docs/19). Kept at
  the top level, not under `wayback`, because the gap-crawl planner uses them
  too.
- `rate_limit_page`: a pattern for the site's disguised 429 page. Also
  top-level: the wget soft-429 handling (docs/19, 0.5) will read the same one.
- `wayback.live_items`: how many items the live site says it has. Wayback's
  listing cannot tell (fictionmania's storyIDs are opaque), so the user types
  it and the card turns it into a coverage percentage.

Instance-wide pacing is in the settings store, because the budget is
archive.org's, not a site's.

Files live under `<site>/import/wayback/`: the raw CDX pages, `survey.json`
and `selection.jsonl.gz`. The archive directory is the source of truth
(docs/03), so none of this is in the database.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Job, Site
from cairn.net.polite import USER_AGENT, Pacing, PoliteClient
from cairn.services import discovery_service, settings_store, storage
from cairn.services import sites as site_service
from cairn.wayback import cdx
from cairn.wayback import survey as wb_survey

JOB_TYPE = "wayback-survey"
HOST = "web.archive.org"
IMPORT_DIR = "import"
RATE_SETTING = "wayback.rate_per_min"
CONTACT_SETTING = "wayback.contact_url"
DEFAULT_RATE_PER_MIN = 10.0


def directory(settings: Settings, site: Site) -> Path:
    return storage.site_dir(settings, site.archive_path) / IMPORT_DIR / "wayback"


def options(site: Site) -> dict[str, Any]:
    stored = site.scope_settings or {}
    wayback = stored.get("wayback") or {}
    return {
        "url_groups": list(stored.get("url_groups") or []),
        "rate_limit_page": stored.get("rate_limit_page") or None,
        "live_items": wayback.get("live_items"),
    }


def set_options(
    site: Site,
    *,
    url_groups: list[dict[str, Any]],
    rate_limit_page: str | None,
    live_items: int | None,
) -> dict[str, Any]:
    """Validate and store. Raises ValueError with a message for the user."""
    groups = wb_survey.parse_groups(url_groups)
    page = (rate_limit_page or "").strip() or None
    if page is not None:
        try:
            re.compile(page)
        except re.error as exc:
            raise ValueError(f"rate-limit page: invalid pattern: {exc}") from exc
    if live_items is not None and live_items < 0:
        raise ValueError("the live item count cannot be negative")

    stored = dict(site.scope_settings or {})
    if groups:
        stored["url_groups"] = [g.to_dict() for g in groups]
    else:
        stored.pop("url_groups", None)
    if page:
        stored["rate_limit_page"] = page
    else:
        stored.pop("rate_limit_page", None)
    wayback = dict(stored.get("wayback") or {})
    if live_items is None:
        wayback.pop("live_items", None)
    else:
        wayback["live_items"] = live_items
    if wayback:
        stored["wayback"] = wayback
    else:
        stored.pop("wayback", None)
    site.scope_settings = stored
    return options(site)


def rate_per_min(session: Session) -> float:
    try:
        rate = float(settings_store.get(session, RATE_SETTING, DEFAULT_RATE_PER_MIN))
    except (TypeError, ValueError):
        return DEFAULT_RATE_PER_MIN
    return min(max(rate, 0.5), 60.0)


def user_agent(session: Session) -> str:
    contact = str(settings_store.get(session, CONTACT_SETTING, "") or "").strip()
    return f"{USER_AGENT[:-1]}; +{contact})" if contact else USER_AGENT


def survey_inputs(session: Session, settings: Settings, site: Site) -> wb_survey.Inputs:
    opts = options(site)
    latest = discovery_service.latest_discovery(session, site.id)
    discovered = (
        discovery_service.read_discovered_urls(settings, site, latest.id) if latest else None
    )
    return wb_survey.Inputs(
        directory=directory(settings, site),
        scope=site_service.resolved_scope(session, site),
        groups=wb_survey.parse_groups(opts["url_groups"]),
        rate_limit_page=opts["rate_limit_page"],
        discovered=discovered,
        rate_per_min=rate_per_min(session),
    )


def cdx_client(agent: str, **hooks: Any) -> PoliteClient:
    return PoliteClient(Pacing(min_gap_s=cdx.CDX_MIN_GAP_S), user_agent=agent, **hooks)


def active_survey(session: Session, site_id: int) -> Job | None:
    return session.scalars(
        select(Job)
        .where(
            Job.site_id == site_id,
            Job.type == JOB_TYPE,
            Job.status.in_(("queued", "running")),
        )
        .limit(1)
    ).first()


def last_survey_job(session: Session, site_id: int) -> Job | None:
    return session.scalars(
        select(Job)
        .where(Job.site_id == site_id, Job.type == JOB_TYPE)
        .order_by(Job.queued_at.desc(), Job.id.desc())
        .limit(1)
    ).first()


def state(session: Session, settings: Settings, site: Site) -> dict[str, Any]:
    """Everything the Wayback card shows, in one read."""
    job = last_survey_job(session, site.id)
    return {
        "options": options(site),
        "rate_per_min": rate_per_min(session),
        "survey": wb_survey.read_report(directory(settings, site)),
        "job": (
            {
                "id": job.id,
                "status": job.status,
                "error": job.error,
                "progress": job.progress,
            }
            if job
            else None
        ),
    }
