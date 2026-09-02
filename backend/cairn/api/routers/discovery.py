"""Discovery and the domain picker (docs/09).

The picker is the feature that most distinguishes this from what exists, so
its endpoints are shaped around the decision the user is actually making:
which hosts to crawl, which to take assets from, and what the consequences
are — not around the tables underneath.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import select

from cairn.api.deps import AppSettings, ClientIp, Csrf, CurrentUser, DbSession
from cairn.api.errors import ApiError
from cairn.api.schemas import JobAccepted, ScopeResponse
from cairn.db.models import Discovery, Job, Site
from cairn.discovery import platform
from cairn.discovery.platform import PRESETS
from cairn.services import audit, discovery_service
from cairn.services import sites as site_service
from cairn.services.scope import ScopeError, to_wget_args

router = APIRouter(tags=["discovery"], dependencies=[Csrf])


def _supervisor(request: Request) -> Any:
    return request.app.state.supervisor


def _require_site(db: DbSession, site_id: int) -> Site:
    site = site_service.get_site(db, site_id)
    if site is None:
        raise ApiError("not_found", "That site does not exist.", status_code=404)
    return site


@router.post(
    "/sites/{site_id}/discover", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED
)
def start_discovery(
    site_id: int,
    db: DbSession,
    user: CurrentUser,
    ip: ClientIp,
    supervisor: Annotated[Any, Depends(_supervisor)],
    max_pages: int = Query(100, ge=1, le=2000),
    max_depth: int = Query(3, ge=1, le=10),
    use_browser: bool = Query(False),
) -> JobAccepted:
    site = _require_site(db, site_id)

    existing = db.scalar(
        select(Job.id).where(Job.site_id == site.id, Job.status.in_(("queued", "running"))).limit(1)
    )
    if existing:
        raise ApiError(
            "already_running",
            "Something is already running for this site.",
            status_code=409,
            detail={"job_id": existing},
        )

    job = supervisor.enqueue(
        db,
        job_type="discovery",
        site_id=site.id,
        spec={"max_pages": max_pages, "max_depth": max_depth, "use_browser": use_browser},
        # Discovery is short and gates everything else, so it jumps the queue
        # ahead of any capture waiting behind it.
        priority=50,
    )
    audit.record(db, "site.discover", actor=user.username, target=site.slug, ip=ip)
    db.commit()
    supervisor.notify()
    return JobAccepted(job_id=job.id)


@router.get("/sites/{site_id}/discovery")
def latest_discovery(site_id: int, db: DbSession, _user: CurrentUser) -> dict[str, Any]:
    """The current picker state: what was found, and what is selected now."""
    from cairn.services import browser

    site = _require_site(db, site_id)
    available, reason = browser.availability()
    # Offered by the picker rather than assumed: rendering is the answer to a
    # domain list that looks too short, and the checkbox is useless if it is
    # switched on in an install with no Chromium.
    browser_state = {"available": available, "reason": reason}

    discovery = discovery_service.latest_discovery(db, site.id)
    if discovery is None:
        return {
            "discovery": None,
            "hosts": [],
            "browser": browser_state,
            "message": "Discovery has not run for this site yet.",
        }

    scope = site_service.resolved_scope(db, site)
    rows = discovery_service.hosts_for(db, discovery.id)

    previous = db.scalars(
        select(Discovery)
        .where(Discovery.site_id == site.id, Discovery.id != discovery.id)
        .order_by(Discovery.started_at.desc())
        .limit(1)
    ).first()

    return {
        "discovery": {
            "id": discovery.id,
            "started_at": discovery.started_at,
            "finished_at": discovery.finished_at,
            "pages_fetched": discovery.pages_fetched,
            "urls_found": discovery.urls_found,
            "summary": discovery.summary or {},
        },
        "hosts": [discovery_service.stat_from_row(row, scope) for row in rows],
        "diff": discovery_service.diff_against(db, discovery, previous),
        "scope_user_edited": bool((site.scope_settings or {}).get("user_edited")),
        "browser": browser_state,
    }


@router.get("/presets")
def list_presets(_user: CurrentUser) -> list[dict[str, Any]]:
    return [preset.to_dict() for preset in PRESETS.values()]


@router.post("/sites/{site_id}/scope/apply-preset", response_model=ScopeResponse)
def apply_preset(
    site_id: int,
    body: dict[str, str],
    db: DbSession,
    settings: AppSettings,
    user: CurrentUser,
    ip: ClientIp,
) -> ScopeResponse:
    """Fold a platform preset into the current scope, reporting what changed."""
    site = _require_site(db, site_id)
    preset = discovery_service.preset_by_id(body.get("preset", ""))
    if preset is None:
        raise ApiError(
            "unknown_preset",
            f"No preset named {body.get('preset')!r}. Known: {', '.join(PRESETS)}.",
            status_code=400,
        )

    scope = site_service.resolved_scope(db, site)
    discovery = discovery_service.latest_discovery(db, site.id)
    hosts_seen = (
        [row.host for row in discovery_service.hosts_for(db, discovery.id)] if discovery else []
    )
    # Preset hosts that discovery never saw are still worth adding: an asset
    # host can be absent from a hundred sampled pages and present on the one
    # post that matters.
    hosts_seen += [p for p in preset.assets_on if not p.startswith("*.")]

    changes = discovery_service.apply_preset_to_scope(
        scope, preset, list(dict.fromkeys(hosts_seen))
    )
    try:
        site_service.save_scope(db, site, scope)
    except ScopeError as exc:
        raise ApiError("scope_invalid", str(exc), status_code=422) from exc

    settings_blob = dict(site.scope_settings or {})
    settings_blob["user_edited"] = True
    settings_blob["preset"] = preset.id
    site.scope_settings = settings_blob

    site_service.write_site_yaml(db, settings, site)
    audit.record(
        db,
        "site.scope",
        actor=user.username,
        target=site.slug,
        ip=ip,
        detail={"preset": preset.id, "changes": len(changes)},
    )

    response = _scope_response(db, site)
    response.notes = [*changes, *response.notes]
    return response


@router.post("/sites/{site_id}/scope/preview")
def preview_scope(site_id: int, db: DbSession, _user: CurrentUser) -> dict[str, Any]:
    """A dry-run estimate, computed from discovery data with no fetching.

    Rough numbers, but they catch the two mistakes that waste hours: a scope
    that quietly includes a neighbouring site, and a missing junk-parameter
    reject (docs/04).
    """
    site = _require_site(db, site_id)
    scope = site_service.resolved_scope(db, site)
    discovery = discovery_service.latest_discovery(db, site.id)

    crawl_hosts = [rule.host for rule in scope.hosts if rule.crawl_pages]
    asset_hosts = [rule.host for rule in scope.asset_only_hosts]

    pages = 0
    excluded = 0
    if discovery is not None:
        sample: list[str] = list((discovery.summary or {}).get("sample_urls") or [])
        rejects = discovery_service.compiled_rejects(scope)
        from urllib.parse import urlsplit

        for url in sample:
            if (urlsplit(url).hostname or "").lower() not in crawl_hosts:
                continue
            if any(pattern.search(url) for pattern in rejects):
                excluded += 1
            else:
                pages += 1
        if discovery.urls_found and sample:
            # The stored sample is capped; scale the ratio up to the real total.
            scale = discovery.urls_found / len(sample)
            pages = int(pages * scale)
            excluded = int(excluded * scale)

    notes: list[str] = []
    try:
        notes = to_wget_args(scope).notes
    except ScopeError as exc:
        notes = [f"This scope cannot run as-is: {exc}"]

    trail = _pagination_outlook(scope, discovery, pages)
    total = pages + (trail["estimated_urls"] if trail and not trail["skipped"] else 0)

    return {
        "pages_to_crawl": pages,
        "excluded_by_pattern": excluded,
        "crawl_hosts": crawl_hosts,
        "asset_hosts": asset_hosts,
        "estimated_bytes": pages * 2_500_000 if pages else 0,
        # The trail is index pages, so it costs time at the politeness rate
        # even where it costs little storage — and time is what ran out.
        "estimated_seconds": int(total * (scope.politeness.get("wait_s") or 1.0)) if total else 0,
        "pagination": trail,
        "notes": notes,
    }


def _pagination_outlook(scope: Any, discovery: Any, posts: int) -> dict[str, Any] | None:
    """What Blogger's Older-posts trail will add, and whether to keep it.

    The cost is **quadratic in post count** — 1.2 index URLs per post on a
    71-post blog, 71.9 on a 2,855-post one — which is the fact the standard
    preset's "keep the trail, it makes the links work" decision was taken
    without. It is still right on a small blog. On a large one the trail *is*
    the capture.

    Whether it is already skipped is asked of the scope rather than inferred
    from which preset was applied: a pattern somebody added by hand counts for
    exactly as much as the lean preset's, and reading the preset id would call
    that scope unprotected.
    """
    summary = getattr(discovery, "summary", None) if discovery is not None else None
    if not isinstance(summary, dict):
        return None
    fingerprint = summary.get("fingerprint")
    if not isinstance(fingerprint, dict) or fingerprint.get("platform") != platform.BLOGGER:
        return None
    if posts <= 0:
        return None

    host = next((rule.host for rule in scope.hosts if rule.crawl_pages), None)
    if host is None:
        return None
    probe = f"https://{host}/search?updated-max=2019-01-01T00:00:00-05:00&max-results=20"
    skipped = any(p.search(probe) for p in discovery_service.compiled_rejects(scope))

    estimate = platform.pagination_estimate(posts)
    return {
        "posts": posts,
        "estimated_urls": estimate,
        "skipped": skipped,
        "recommend_lean": not skipped and posts > platform.LEAN_RECOMMENDED_ABOVE,
        "measured": [{"posts": p, "urls": u} for p, u in platform.PAGINATION_MEASURED],
    }


def _scope_response(db: DbSession, site: Site) -> ScopeResponse:
    from cairn.api.routers.sites import _scope_response as build

    return build(db, site)
