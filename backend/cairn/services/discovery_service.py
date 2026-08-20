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
from cairn.discovery.platform import PRESETS, CompanionPass, Preset, matches_host_pattern
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
    """Attach discovered feeds to the site, without duplicating existing ones.

    A comment feed arrives switched off. docs/08 asks for every discovered feed
    to be a checklist rather than added silently, and that is right about
    comment feeds — hundreds of entries pointing at fragments of pages the
    posts feed already covers, which after M6 means real requests and real
    captures. It is wrong about the posts feed: it is the reason the site is
    being archived, and making somebody tick a box to get the obvious thing is
    friction, not consent. So both are recorded and the noisy one is off, which
    is the same list either way — the difference is only what happens if
    nobody looks at it.
    """
    from cairn.services import feeds as feed_service

    known = set(session.scalars(select(Feed.url).where(Feed.site_id == site.id)).all())
    added = 0
    for url in result.feeds:
        if url in known:
            continue
        comments = feed_service.is_comment_feed(url)
        session.add(
            Feed(
                site_id=site.id,
                url=url,
                kind="auto",
                enabled=not comments,
                auto_capture=not comments,
                interval_min=feed_service.DEFAULT_INTERVAL_MIN,
                # Due now rather than one interval from now: discovery has just
                # proved the feed answers, and the first poll is what turns its
                # existing entries into a baseline instead of a backlog.
                next_poll_at=utcnow(),
                disabled_reason=(
                    "Comment feeds are off by default: they are mostly noise, and their "
                    "entries point at pages the posts feed already covers."
                    if comments
                    else None
                ),
            )
        )
        added += 1
    if added:
        session.flush()
    return added


def scope_from_result(
    result: DiscoveryResult,
    *,
    seed_url: str,
    apply_preset: bool = True,
    extra_seeds: list[str] | None = None,
) -> Scope:
    """Turn the picker's default selections into a resolved scope."""
    scope = Scope(seeds=list(dict.fromkeys([seed_url, *(extra_seeds or [])])))
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

    # Every seed's host is crawlable, whatever the classifier decided. A seed
    # the scope would refuse on the first request looks exactly like a site
    # that is down, and discovery can classify a second domain as an asset host
    # when the first one embeds its images — which is true and is not a reason
    # to stop crawling the place the user asked us to start from.
    by_host = {rule.host: rule for rule in scope.hosts}
    for seed in scope.seeds:
        host = (urlsplit(seed).hostname or "").lower()
        if not host:
            continue
        rule = by_host.get(host)
        if rule is None:
            scope.hosts.insert(0, HostRule(host, crawl_pages=True, fetch_assets=True))
        else:
            rule.crawl_pages = True

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

    # Retired first, so a pattern that is both retired and re-added in a
    # different form ends up in its current shape rather than its old one.
    for pattern in preset.retired_patterns:
        if pattern in scope.reject_patterns:
            scope.reject_patterns.remove(pattern)
            changes.append(f"removed reject {pattern} (the preset no longer recommends it)")

    existing = set(scope.reject_patterns)
    for pattern, note in preset.reject_patterns:
        if pattern not in existing:
            scope.reject_patterns.append(pattern)
            changes.append(f"added reject {pattern} ({note})")
    return changes


def preset_by_id(preset_id: str) -> Preset | None:
    return PRESETS.get(preset_id)


def companion_pass_for(site: Site) -> CompanionPass | None:
    """The second capture this site's preset offers, if it offers one.

    Read from the preset that was actually *applied*, recorded in
    `scope_settings`, rather than from what the site fingerprints as. A scope
    somebody built by hand has no preset and gets no companion pass, because
    the pass lifts specific rejects and re-adds them as an accept rule — doing
    that to a scope nobody declared would be rewriting a boundary its author
    chose.
    """
    preset = PRESETS.get(str((site.scope_settings or {}).get("preset") or ""))
    return preset.companion_pass if preset else None


def seeds_for_capture(
    session: Session, site: Site, scope: Scope
) -> tuple[list[str], dict[str, int]]:
    """Every URL the crawler should start from, and where each came from.

    This is the mechanism that sidesteps the depth ceiling: the crawler gets
    the complete URL set from sitemaps and feeds up front, so link-following is
    a supplement rather than the way content is found (docs/05).
    """
    from cairn.services import sites as site_service

    seeds = site_service.all_seeds(site)
    counts = {"manual": len(seeds), "sitemap": 0, "feed": 0, "css_escaped": 0}

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
    for pattern in scope.all_reject_patterns:
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


def applied_preset(site: Site) -> dict[str, str] | None:
    """The preset whose rules this site's scope was built from, if any.

    Same source as `companion_pass_for` and for the same reason: what was
    *applied*, not what the site fingerprints as. A scope somebody built by
    hand reports None, and "no preset" is a real answer worth showing rather
    than a gap to fill with a guess.
    """
    preset = PRESETS.get(str((site.scope_settings or {}).get("preset") or ""))
    if preset is None:
        return None
    return {"id": preset.id, "name": preset.name}
