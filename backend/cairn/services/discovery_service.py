"""Persisting a discovery run, and turning its result into a scope.

The runner does the network work and knows nothing about the database; this
module owns the rows and the translation into the scope the engine will
enforce. Keeping the split means discovery can be tested against fixtures
without a database, and re-run without touching one.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.db.models import DiscoveredHost, Discovery, Feed, Site
from cairn.db.types import utcnow
from cairn.discovery import hosts as host_classify
from cairn.discovery.platform import PRESETS, Preset, matches_host_pattern
from cairn.discovery.runner import DiscoveryResult
from cairn.logging import get_logger
from cairn.services.scope import HostRule, Scope

log = get_logger(__name__)

# Sitemaps and feeds are the complete URL list; handing them to the crawler up
# front is what removes the depth ceiling. Bounded so a 100k-post site does not
# produce a seed file the engine chokes on.
MAX_SEEDS = 50_000


def persist(session: Session, site: Site, result: DiscoveryResult, job_id: int | None) -> Discovery:
    """Record a run and its hosts, replacing that run's rows only.

    Previous discoveries are kept: re-running is how scope drift gets noticed,
    and a diff needs something to diff against (docs/04).
    """
    discovery = Discovery(
        site_id=site.id,
        job_id=job_id,
        started_at=utcnow(),
        finished_at=utcnow(),
        pages_fetched=result.pages_fetched,
        urls_found=len(result.all_urls),
        summary={
            **result.summary(),
            # The URL list itself can be enormous; keep a bounded sample in the
            # row and re-derive the rest from the sources when capturing.
            "sample_urls": result.all_urls[:200],
        },
    )
    session.add(discovery)
    session.flush()

    for stat in result.hosts:
        session.add(
            DiscoveredHost(
                discovery_id=discovery.id,
                host=stat.host,
                registrable=stat.registrable,
                is_seed_host=stat.is_seed_host,
                link_refs=stat.link_refs,
                asset_refs=stat.asset_refs,
                distinct_urls=stat.distinct_urls,
                role_guess=stat.role,
                sample_urls=stat.sample_urls[:5],
            )
        )

    if site.status == "new":
        site.status = "indexed"
    if result.title and site.title == site.primary_host:
        # The user did not name it, so use the blog's own title rather than
        # leaving a hostname in the sites list.
        site.title = result.title
    site.updated_at = utcnow()
    session.flush()
    return discovery


def record_feeds(session: Session, site: Site, result: DiscoveryResult) -> int:
    """Attach discovered feeds to the site, without duplicating existing ones."""
    known = set(session.scalars(select(Feed.url).where(Feed.site_id == site.id)).all())
    added = 0
    for url in result.feeds:
        if url in known:
            continue
        session.add(Feed(site_id=site.id, url=url, kind="auto", enabled=True))
        added += 1
    if added:
        session.flush()
    return added


def scope_from_result(
    result: DiscoveryResult, *, seed_url: str, apply_preset: bool = True
) -> Scope:
    """Turn the picker's default selections into a resolved scope."""
    scope = Scope(seeds=[seed_url])
    for stat in result.hosts:
        if not (stat.crawl_pages or stat.fetch_assets):
            continue
        scope.hosts.append(
            HostRule(
                host=stat.host,
                crawl_pages=stat.crawl_pages,
                fetch_assets=stat.fetch_assets,
                allow_extensionless=stat.allow_extensionless,
            )
        )

    if not any(h.crawl_pages for h in scope.hosts):
        # Discovery found nothing crawlable, which can happen when the landing
        # page redirects to another host. The seed host is always crawlable —
        # a scope with nothing to start from archives nothing at all.
        seed_host = (urlsplit(seed_url).hostname or "").lower()
        scope.hosts.insert(0, HostRule(seed_host, crawl_pages=True, fetch_assets=True))

    preset = result.fingerprint.preset if apply_preset else None
    if preset:
        scope.reject_patterns = [pattern for pattern, _note in preset.reject_patterns]
    return scope


def apply_preset_to_scope(scope: Scope, preset: Preset, hosts_seen: list[str]) -> list[str]:
    """Fold a preset into an existing scope. Returns what changed, for the UI."""
    changes: list[str] = []
    by_host = {rule.host: rule for rule in scope.hosts}

    for host in hosts_seen:
        rule = by_host.get(host)
        if any(matches_host_pattern(p, host) for p in preset.hosts_off):
            if rule and (rule.crawl_pages or rule.fetch_assets):
                rule.crawl_pages = rule.fetch_assets = False
                changes.append(f"turned off {host}")
            continue
        if any(matches_host_pattern(p, host) for p in preset.assets_on):
            extensionless = any(matches_host_pattern(p, host) for p in preset.extensionless_ok)
            if rule is None:
                rule = HostRule(host, crawl_pages=False, fetch_assets=True)
                scope.hosts.append(rule)
                by_host[host] = rule
                changes.append(f"added {host} as assets-only")
            elif not rule.fetch_assets:
                rule.fetch_assets = True
                changes.append(f"enabled assets for {host}")
            if extensionless and not rule.allow_extensionless:
                rule.allow_extensionless = True
                changes.append(f"allowed extension-less URLs on {host}")

    existing = set(scope.reject_patterns)
    for pattern, note in preset.reject_patterns:
        if pattern not in existing:
            scope.reject_patterns.append(pattern)
            changes.append(f"added reject {pattern} ({note})")
    return changes


def preset_by_id(preset_id: str) -> Preset | None:
    return PRESETS.get(preset_id)


def seeds_for_capture(
    session: Session, site: Site, scope: Scope
) -> tuple[list[str], dict[str, int]]:
    """Every URL the crawler should start from, and where each came from.

    This is the mechanism that sidesteps the depth ceiling: the crawler gets
    the complete URL set from sitemaps and feeds up front, so link-following is
    a supplement rather than the way content is found (docs/05).
    """
    seeds = [site.seed_url]
    counts = {"manual": 1, "sitemap": 0, "feed": 0, "css_escaped": 0}

    latest = session.scalars(
        select(Discovery)
        .where(Discovery.site_id == site.id)
        .order_by(Discovery.started_at.desc())
        .limit(1)
    ).first()
    if latest is None or not latest.summary:
        return seeds, counts

    stored: list[str] = list((latest.summary or {}).get("sample_urls") or [])
    allowed = {rule.host for rule in scope.hosts if rule.crawl_pages}
    rejects = compiled_rejects(scope)

    for url in stored:
        if len(seeds) >= MAX_SEEDS:
            break
        host = (urlsplit(url).hostname or "").lower()
        if host not in allowed or url in seeds:
            continue
        # A seed the scope would reject anyway is a wasted fetch and a
        # confusing error in the URL list.
        if any(pattern.search(url) for pattern in rejects):
            continue
        seeds.append(url)
        counts["sitemap"] += 1

    # Assets the crawler provably cannot reach on its own. wget requests the
    # still-escaped text against the page's own host, 404s, and never learns
    # the real URL exists — so unless it is handed over here, the asset is
    # lost no matter how the scope is set. Filtered by `fetch_assets` rather
    # than `crawl_pages`: these are images and stylesheets on hosts nobody
    # wants crawled as websites.
    asset_hosts = {rule.host for rule in scope.hosts if rule.fetch_assets}
    for url in list((latest.summary or {}).get("escaped_assets") or []):
        if len(seeds) >= MAX_SEEDS:
            break
        host = (urlsplit(url).hostname or "").lower()
        if host not in asset_hosts or url in seeds:
            continue
        seeds.append(url)
        counts["css_escaped"] += 1

    return seeds, counts


def compiled_rejects(scope: Scope) -> list[Any]:
    import re

    compiled = []
    for pattern in scope.reject_patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error:  # pragma: no cover — validated on save
            continue
    return compiled


def latest_discovery(session: Session, site_id: int) -> Discovery | None:
    return session.scalars(
        select(Discovery)
        .where(Discovery.site_id == site_id)
        .order_by(Discovery.started_at.desc())
        .limit(1)
    ).first()


def hosts_for(session: Session, discovery_id: int) -> list[DiscoveredHost]:
    return list(
        session.scalars(
            select(DiscoveredHost)
            .where(DiscoveredHost.discovery_id == discovery_id)
            .order_by(DiscoveredHost.is_seed_host.desc(), DiscoveredHost.asset_refs.desc())
        ).all()
    )


def diff_against(
    session: Session, current: Discovery, previous: Discovery | None
) -> dict[str, list[str]]:
    """New and disappeared hosts between two runs.

    Re-running discovery on an established site is how scope drift gets
    noticed — a blog that started embedding a new CDN, or an asset host that
    went away and left existing captures pointing at nothing (docs/04).
    """
    if previous is None:
        return {"new_hosts": [], "gone_hosts": []}
    now = {h.host for h in hosts_for(session, current.id)}
    before = {h.host for h in hosts_for(session, previous.id)}
    return {
        "new_hosts": sorted(now - before),
        "gone_hosts": sorted(before - now),
    }


def stat_from_row(row: DiscoveredHost, scope: Scope) -> dict[str, Any]:
    """A picker row, with the site's current selection applied."""
    rule = next((r for r in scope.hosts if r.host == row.host), None)
    return {
        "host": row.host,
        "registrable": row.registrable,
        "is_seed_host": row.is_seed_host,
        "link_refs": row.link_refs,
        "asset_refs": row.asset_refs,
        "distinct_urls": row.distinct_urls,
        "role": row.role_guess or host_classify.ROLE_UNKNOWN,
        "sample_urls": row.sample_urls or [],
        "crawl_pages": bool(rule and rule.crawl_pages),
        "fetch_assets": bool(rule and rule.fetch_assets),
        "allow_extensionless": bool(rule and rule.allow_extensionless),
    }
