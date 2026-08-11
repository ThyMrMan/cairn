"""The discovery job: probe, enumerate, sample, classify (docs/04).

Three phases in a deliberate order. Probing the origin identifies the platform,
which unlocks a preset that supplies the right paths for everything after it.
Enumeration from sitemaps and feeds is preferred over crawling because it is
complete and cheap. Only then does a bounded sampling crawl run, and only to
learn *which hosts serve subresources* — not to find content.

Discovery writes no WARCs and can be re-run at any time, which is what lets it
be cheap enough to schedule independently of capture ([D10](../docs/00-decisions.md)).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

from cairn.discovery import hosts as host_classify
from cairn.discovery import sources
from cairn.discovery.fetch import USER_AGENT, Fetcher
from cairn.discovery.platform import Fingerprint, fingerprint
from cairn.logging import get_logger
from cairn.services.htmlrefs import parse_page

log = get_logger(__name__)

DEFAULT_MAX_PAGES = 100
DEFAULT_MAX_DEPTH = 3
DEFAULT_CONCURRENCY = 2

# A skin's escaped URLs repeat on every page, so this de-duplicates to a
# handful in practice. The cap is for the site that generates them per-post.
MAX_ESCAPED_ASSETS = 2_000

ProgressFn = Callable[[str, dict[str, Any]], None]


@dataclass(slots=True)
class DiscoveryOptions:
    max_pages: int = DEFAULT_MAX_PAGES
    max_depth: int = DEFAULT_MAX_DEPTH
    concurrency: int = DEFAULT_CONCURRENCY
    wait_s: float = 0.0
    user_agent: str | None = None
    cookies_file: str | None = None
    follow_sitemaps: bool = True
    follow_feeds: bool = True


@dataclass(slots=True)
class DiscoveryResult:
    seed_url: str
    seed_host: str
    fingerprint: Fingerprint = field(default_factory=Fingerprint)
    robots: sources.Robots = field(default_factory=sources.Robots)
    sitemaps: list[str] = field(default_factory=list)
    feeds: list[str] = field(default_factory=list)
    sitemap_urls: list[str] = field(default_factory=list)
    feed_urls: list[str] = field(default_factory=list)
    pages_fetched: int = 0
    hosts: list[host_classify.HostStat] = field(default_factory=list)
    # Assets a crawler that cannot decode CSS escapes will never request.
    # Carried through to the capture as seeds; see `_add_asset` in htmlrefs.
    escaped_assets: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    title: str | None = None

    @property
    def all_urls(self) -> list[str]:
        return list(dict.fromkeys([*self.sitemap_urls, *self.feed_urls]))

    def summary(self) -> dict[str, Any]:
        return {
            "seed_url": self.seed_url,
            "seed_host": self.seed_host,
            "title": self.title,
            "fingerprint": self.fingerprint.to_dict(),
            "robots": {
                "fetched": self.robots.fetched,
                "sitemaps": self.robots.sitemaps,
                "disallowed": self.robots.disallowed[:50],
            },
            "sitemaps": self.sitemaps,
            "feeds": self.feeds,
            "urls_from_sitemaps": len(self.sitemap_urls),
            "urls_from_feeds": len(self.feed_urls),
            "escaped_assets": list(self.escaped_assets),
            "pages_fetched": self.pages_fetched,
            "errors": self.errors[:50],
            "warnings": self.warnings,
        }


async def discover(
    seed_url: str,
    options: DiscoveryOptions | None = None,
    progress: ProgressFn | None = None,
) -> DiscoveryResult:
    options = options or DiscoveryOptions()
    parts = urlsplit(seed_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    result = DiscoveryResult(seed_url=seed_url, seed_host=(parts.hostname or "").lower())

    def report(stage: str, **fields: Any) -> None:
        if progress is not None:
            progress(stage, fields)

    async with Fetcher(
        user_agent=options.user_agent or USER_AGENT,
        wait_s=options.wait_s,
        cookies_file=options.cookies_file,
    ) as fetcher:
        # ── phase 1: probe the origin ────────────────────────────────────
        report("probe", message="reading robots.txt and the home page")
        landing = await fetcher.get(seed_url)
        if not landing.ok:
            result.errors.append(f"could not fetch {seed_url}: {landing.error or landing.status}")
            report("failed", message=result.errors[-1])
            return result

        page = parse_page(landing.body, str(landing.url))
        result.title = page.title
        result.fingerprint = fingerprint(url=seed_url, body=landing.body, generator=page.generator)
        preset = result.fingerprint.preset
        report(
            "fingerprint",
            platform=result.fingerprint.platform,
            confidence=result.fingerprint.confidence,
        )

        result.robots = await sources.fetch_robots(fetcher, origin)

        # ── phase 2: enumerate from authoritative sources ────────────────
        if options.follow_sitemaps:
            candidates = list(result.robots.sitemaps)
            candidates += [
                urljoin(origin, p)
                for p in (preset.sitemap_paths if preset else sources.SITEMAP_CANDIDATES)
            ]
            report("sitemaps", message=f"checking {len(candidates)} sitemap location(s)")
            found = await sources.collect_sitemaps(fetcher, _dedupe(candidates))
            result.sitemaps = found.documents
            result.sitemap_urls = found.urls
            result.errors += found.errors
            report("sitemaps", urls=len(found.urls), documents=len(found.documents))

        if options.follow_feeds:
            feed_candidates = list(page.feeds)
            feed_candidates += [
                urljoin(origin, p)
                for p in (preset.feed_paths if preset else sources.FEED_CANDIDATES)
            ]
            report("feeds", message=f"checking {len(feed_candidates)} feed location(s)")
            for candidate in _dedupe(feed_candidates):
                parsed = await sources.collect_feed(fetcher, candidate)
                if parsed.ok:
                    result.feeds.append(parsed.url)
                    result.feed_urls += parsed.entries
            result.feed_urls = _dedupe(result.feed_urls)
            report("feeds", feeds=len(result.feeds), urls=len(result.feed_urls))

        if not result.sitemap_urls and not result.feed_urls:
            result.warnings.append(
                "No sitemap or feed was found, so the URL list comes only from the "
                "sampling crawl and may be incomplete. Check the capture's URL count "
                "against what you expect."
            )

        # ── phase 3: bounded sampling crawl ──────────────────────────────
        report("sampling", message="sampling pages to find asset hosts")
        counts = await _sample(fetcher, seed_url, result, options, landing_page=page, report=report)

    result.hosts = host_classify.classify(
        seed_host=result.seed_host,
        link_refs=counts.link_refs,
        asset_refs=counts.asset_refs,
        urls_by_host=counts.urls_by_host,
        mime_by_host=counts.mime_by_host,
    )
    host_classify.apply_defaults(result.hosts, preset)
    result.escaped_assets = sorted(counts.escaped_assets)[:MAX_ESCAPED_ASSETS]

    if result.escaped_assets:
        result.warnings.append(
            f"{len(result.escaped_assets)} asset(s) are referenced only through "
            "CSS escape sequences, which wget cannot decode. The capture will be "
            "given the decoded URLs directly so they are archived anyway."
        )
    if counts.lazy_hints:
        result.warnings.append(
            f"{counts.lazy_hints} lazy-loaded image reference(s) seen while sampling. "
            "wget cannot execute JavaScript, so a browser engine will be needed for "
            "those images."
        )
    if len(result.hosts) <= 1 and result.pages_fetched > 3:
        result.warnings.append(
            "Almost no subresource hosts were found. If this site builds its pages "
            "with JavaScript, discovery cannot see what it loads and the domain list "
            "here will be incomplete."
        )

    report(
        "done",
        hosts=len(result.hosts),
        urls=len(result.all_urls),
        pages=result.pages_fetched,
    )
    return result


@dataclass(slots=True)
class _Counts:
    link_refs: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    asset_refs: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    urls_by_host: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    mime_by_host: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(int))
    )
    escaped_assets: set[str] = field(default_factory=set)
    lazy_hints: int = 0


async def _sample(
    fetcher: Fetcher,
    seed_url: str,
    result: DiscoveryResult,
    options: DiscoveryOptions,
    *,
    landing_page: Any,
    report: Callable[..., None],
) -> _Counts:
    """Fetch a bounded sample of pages and count what they reference.

    Link-following never leaves the seed host. Discovery counts other hosts;
    it does not explore them. Sampling a hundred pages is enough to see every
    asset host a template uses, and wandering off-host to find more would be
    the very behaviour the scope model exists to prevent.
    """
    counts = _Counts()
    seed_host = result.seed_host
    seen: set[str] = {seed_url}
    queue: list[tuple[str, int]] = []

    def absorb(page: Any) -> None:
        counts.lazy_hints += page.lazy_hints
        counts.escaped_assets |= page.escaped_assets
        for link in page.links:
            host = host_classify.host_of(link)
            # A link straight to an image is not a link to a page. Blogger wraps
            # every post image in <a href=".../s1600/image.jpg"> for its
            # lightbox, so counting those as page references makes the image CDN
            # look like a site somebody might want to crawl — and pushes it out
            # of the assets-only default that is the correct answer for it.
            if host_classify.looks_like_asset(link):
                counts.asset_refs[host] += 1
            else:
                counts.link_refs[host] += 1
            counts.urls_by_host[host].add(link)
        for asset in page.assets:
            host = host_classify.host_of(asset)
            counts.asset_refs[host] += 1
            counts.urls_by_host[host].add(asset)

    absorb(landing_page)
    result.pages_fetched = 1
    for link in landing_page.links:
        if host_classify.host_of(link) == seed_host:
            queue.append((link, 1))

    # Sitemap URLs make a better sample than link-following: they are the real
    # pages rather than whatever the template happens to link from the home
    # page, and they cost nothing extra to obtain.
    for url in result.sitemap_urls[:200]:
        if host_classify.host_of(url) == seed_host:
            queue.append((url, 1))

    semaphore = asyncio.Semaphore(max(1, options.concurrency))

    async def visit(url: str, depth: int) -> list[tuple[str, int]]:
        async with semaphore:
            fetched = await fetcher.get(url)
        host = host_classify.host_of(url)
        mime = fetched.content_type.split(";")[0].strip().lower()
        if mime:
            counts.mime_by_host[host][mime] += 1
        if not fetched.ok or not fetched.is_html:
            return []
        page = parse_page(fetched.body, str(fetched.url))
        absorb(page)
        for feed in page.feeds:
            if feed not in result.feeds:
                result.feeds.append(feed)
        if depth >= options.max_depth:
            return []
        return [
            (link, depth + 1) for link in page.links if host_classify.host_of(link) == seed_host
        ]

    while queue and result.pages_fetched < options.max_pages:
        batch: list[tuple[str, int]] = []
        while queue and len(batch) < options.concurrency:
            url, depth = queue.pop(0)
            key = url.split("#")[0]
            if key in seen:
                continue
            seen.add(key)
            batch.append((url, depth))
        if not batch:
            break

        results = await asyncio.gather(
            *(visit(url, depth) for url, depth in batch), return_exceptions=True
        )
        result.pages_fetched += len(batch)
        for item in results:
            if isinstance(item, BaseException):
                result.errors.append(str(item)[:200])
                continue
            queue += item
        report("sampling", pages=result.pages_fetched, hosts=len(counts.urls_by_host))

    return counts


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


async def probe_only(seed_url: str, options: DiscoveryOptions | None = None) -> Fingerprint:
    """Just the fingerprint — used when adding a site, before a full run."""
    options = options or DiscoveryOptions()
    async with Fetcher(
        user_agent=options.user_agent or USER_AGENT,
        cookies_file=options.cookies_file,
    ) as fetcher:
        landing = await fetcher.get(seed_url)
        if not landing.ok:
            return Fingerprint()
        page = parse_page(landing.body, str(landing.url))
        return fingerprint(url=seed_url, body=landing.body, generator=page.generator)


AsyncDiscover = Callable[[str, DiscoveryOptions | None], Awaitable[DiscoveryResult]]
