"""Authoritative URL sources: robots.txt, sitemaps, feeds (docs/04 phase 2).

Enumerating from these beats crawling for them. It is complete, it is cheap,
and it is the mechanism that removes ArchiveBox's depth ceiling entirely — the
crawler is handed the full URL set up front, so link-following becomes a
supplement rather than the way content is found.

XML here is attacker-controlled, so it is parsed with `defusedxml`. The stdlib
parsers expand entities by default, and a sitemap is exactly the kind of file
someone would use to try it (docs/11).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

from defusedxml import ElementTree as DefusedET

from cairn.discovery.fetch import Fetched, Fetcher
from cairn.logging import get_logger

log = get_logger(__name__)

MAX_SITEMAP_DOCS = 60
MAX_SITEMAP_DEPTH = 4
MAX_URLS = 100_000
MAX_FEED_PAGES = 40
BLOGGER_FEED_PAGE = 500

SITEMAP_CANDIDATES = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml", "/sitemap.txt")
FEED_CANDIDATES = (
    "/feeds/posts/default",  # Blogger
    "/feed",
    "/rss",
    "/rss.xml",
    "/atom.xml",
    "/feed.xml",
    "/index.xml",
)

_SITEMAP_DIRECTIVE = re.compile(r"^\s*sitemap\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)
_DISALLOW = re.compile(r"^\s*disallow\s*:\s*(\S*)", re.IGNORECASE | re.MULTILINE)
_USER_AGENT = re.compile(r"^\s*user-agent\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)


@dataclass(slots=True)
class Robots:
    fetched: bool = False
    sitemaps: list[str] = field(default_factory=list)
    disallowed: list[str] = field(default_factory=list)

    def blocks(self, path: str) -> bool:
        return any(path.startswith(rule) for rule in self.disallowed if rule)


@dataclass(slots=True)
class UrlSource:
    """URLs found, and where they came from."""

    urls: list[str] = field(default_factory=list)
    lastmod: dict[str, str] = field(default_factory=dict)
    documents: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def fetch_robots(fetcher: Fetcher, origin: str) -> Robots:
    """Read robots.txt for its Sitemap directives and its rules.

    Rules are recorded, not obeyed here — obeying them is the crawl's decision
    and the user's toggle. Blogger disallows `/search`, which is where label
    pages live, so a discovery pass that silently skipped them would hide
    content the user explicitly wants.
    """
    result = await fetcher.get(urljoin(origin, "/robots.txt"))
    robots = Robots(fetched=result.ok)
    if not result.ok:
        return robots

    text = result.text()
    declared = (urljoin(origin, m.group(1).strip()) for m in _SITEMAP_DIRECTIVE.finditer(text))
    robots.sitemaps = [url for url in declared if url.startswith(("http://", "https://"))]

    # Only the rules that apply to everyone; a block aimed at one named bot is
    # not a statement about us.
    for block in re.split(r"\n\s*\n", text):
        agents = [m.group(1).strip() for m in _USER_AGENT.finditer(block)]
        if not agents or "*" in agents:
            robots.disallowed += [
                m.group(1).strip() for m in _DISALLOW.finditer(block) if m.group(1).strip()
            ]
    robots.disallowed = sorted(set(robots.disallowed))
    return robots


# ── sitemaps ─────────────────────────────────────────────────────────────


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap(body: bytes) -> tuple[list[tuple[str, str | None]], list[str]]:
    """Return (urls with lastmod, child sitemap URLs).

    Handles `<urlset>` and `<sitemapindex>`, and tolerates either being served
    with an unexpected namespace or none at all.
    """
    try:
        root = DefusedET.fromstring(body)
    except Exception as exc:
        # Any XML failure — malformed, wrong content type, an entity-expansion
        # attempt defusedxml refused — means the same thing here: not a sitemap.
        raise ValueError(f"not parseable as XML: {exc}") from exc

    # A great many servers answer a missing sitemap with 200 and an HTML error
    # page, which is well-formed XML often enough to parse cleanly and yield
    # nothing. Reporting that as an empty sitemap is indistinguishable from a
    # site that genuinely has none, so say which it was.
    root_tag = _strip_ns(root.tag)
    if root_tag not in ("urlset", "sitemapindex"):
        raise ValueError(f"not parseable as a sitemap: root element is <{root_tag}>")

    urls: list[tuple[str, str | None]] = []
    children: list[str] = []
    for element in root.iter():
        tag = _strip_ns(element.tag)
        if tag not in ("url", "sitemap"):
            continue
        loc = None
        lastmod = None
        for child in element:
            child_tag = _strip_ns(child.tag)
            if child_tag == "loc" and child.text:
                loc = child.text.strip()
            elif child_tag == "lastmod" and child.text:
                lastmod = child.text.strip()
        if not loc:
            continue
        if tag == "sitemap":
            children.append(loc)
        else:
            urls.append((loc, lastmod))
    return urls, children


async def collect_sitemaps(fetcher: Fetcher, roots: list[str]) -> UrlSource:
    """Walk sitemaps breadth-first, bounded, with a visited set.

    Sitemap indexes can point at each other, and a cycle would otherwise spin
    until the job is killed. The visited set is on the *normalized* URL so a
    trailing-slash variant does not slip past it.
    """
    source = UrlSource()
    seen: set[str] = set()
    queue: list[tuple[str, int]] = [(url, 0) for url in roots]

    while queue and len(source.documents) < MAX_SITEMAP_DOCS and len(source.urls) < MAX_URLS:
        url, depth = queue.pop(0)
        key = _normalize(url)
        if key in seen or depth > MAX_SITEMAP_DEPTH:
            continue
        seen.add(key)

        result = await fetcher.get(url)
        if not result.ok:
            source.errors.append(f"{url}: {result.error or result.status}")
            continue
        source.documents.append(url)

        if url.endswith(".txt") or result.content_type.startswith("text/plain"):
            for line in result.text().splitlines():
                candidate = line.strip()
                if candidate.startswith(("http://", "https://")):
                    source.urls.append(candidate)
            continue

        try:
            urls, children = parse_sitemap(result.body)
        except ValueError as exc:
            source.errors.append(f"{url}: {exc}")
            continue

        for loc, lastmod in urls:
            source.urls.append(loc)
            if lastmod:
                source.lastmod[loc] = lastmod
        queue += [(child, depth + 1) for child in children]

        # Blogger caps each sitemap at ~150 URLs and paginates with ?page=N,
        # which is not part of the sitemap spec and is invisible to a naive
        # walker — the first page looks like the whole thing.
        if urls and _looks_paginated(url):
            queue += await _blogger_sitemap_pages(fetcher, url, source, seen)

    source.urls = _dedupe(source.urls)
    return source


def _looks_paginated(url: str) -> bool:
    parts = urlsplit(url)
    return parts.path.endswith("sitemap.xml") and "page=" not in (parts.query or "")


async def _blogger_sitemap_pages(
    fetcher: Fetcher, base: str, source: UrlSource, seen: set[str]
) -> list[tuple[str, int]]:
    """Follow ?page=2,3,… until a page adds nothing new.

    Stopping on "no URLs" alone is not enough. `?page=N` is a Blogger
    convention, not part of the sitemap spec, and a server that has never
    heard of it simply ignores the parameter and serves page 1 again — so the
    loop happily fetches the same document sixty times and reports sixty
    sitemaps. Stopping when a page contributes no *new* URL covers both the
    real end of pagination and the server that was never paginating.
    """
    found: list[tuple[str, int]] = []
    known = set(source.urls)
    for page in range(2, MAX_SITEMAP_DOCS):
        candidate = _with_query(base, f"page={page}")
        if _normalize(candidate) in seen:
            break
        result = await fetcher.get(candidate)
        if not result.ok:
            break
        try:
            urls, _children = parse_sitemap(result.body)
        except ValueError:
            break
        if not urls:
            break

        fresh = [(loc, lastmod) for loc, lastmod in urls if loc not in known]
        if not fresh:
            break

        seen.add(_normalize(candidate))
        source.documents.append(candidate)
        for loc, lastmod in fresh:
            source.urls.append(loc)
            known.add(loc)
            if lastmod:
                source.lastmod[loc] = lastmod
        if len(source.urls) >= MAX_URLS:
            break
    return found


# ── feeds ────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class FeedResult:
    url: str
    kind: str = "unknown"
    title: str | None = None
    entries: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.entries)


def parse_feed(body: bytes, url: str) -> FeedResult:
    """Parse with feedparser, which handles Atom's `<link href>` correctly.

    That form is the ArchiveBox gap from the original evaluation: a naive
    reader looks for `<link>` *text* and finds nothing, so an Atom feed — which
    is Blogger's default — appears to contain no URLs at all.
    """
    import feedparser

    parsed = feedparser.parse(body)
    result = FeedResult(url=url, kind=str(parsed.get("version") or "unknown"))
    feed_meta = parsed.get("feed") or {}
    result.title = (feed_meta.get("title") or None) if hasattr(feed_meta, "get") else None

    for entry in parsed.get("entries") or []:
        link = entry.get("link")
        if not link:
            for candidate in entry.get("links") or []:
                if candidate.get("rel") in (None, "alternate") and candidate.get("href"):
                    link = candidate["href"]
                    break
        if link:
            result.entries.append(link)

    if not result.entries and parsed.get("bozo"):
        result.error = str(parsed.get("bozo_exception") or "not a parseable feed")[:200]
    return result


async def collect_feed(fetcher: Fetcher, url: str, *, paginate: bool = True) -> FeedResult:
    """Read a feed, following pagination so the archive window is not the feed's."""
    result = await fetcher.get(url)
    if not result.ok:
        return FeedResult(url=url, error=result.error or f"HTTP {result.status}")

    parsed = parse_feed(result.body, url)
    if not paginate or not parsed.ok:
        return parsed

    if _is_blogger_feed(url):
        parsed.entries += await _blogger_feed_pages(fetcher, url, len(parsed.entries))
    else:
        parsed.entries += await _rfc5005_pages(fetcher, result)

    parsed.entries = _dedupe(parsed.entries)
    return parsed


def _is_blogger_feed(url: str) -> bool:
    return "/feeds/posts/" in urlsplit(url).path


async def _blogger_feed_pages(fetcher: Fetcher, url: str, first_count: int) -> list[str]:
    """Blogger paginates with start-index/max-results.

    It silently caps max-results at 500, so the count returned is what decides
    whether to continue — trusting the parameter reads the same page forever.
    """
    entries: list[str] = []
    start = first_count + 1
    for _ in range(MAX_FEED_PAGES):
        candidate = _with_query(url, f"start-index={start}&max-results={BLOGGER_FEED_PAGE}")
        result = await fetcher.get(candidate)
        if not result.ok:
            break
        page = parse_feed(result.body, candidate)
        if not page.entries:
            break
        entries += page.entries
        start += len(page.entries)
        if len(page.entries) < BLOGGER_FEED_PAGE:
            break
    return entries


async def _rfc5005_pages(fetcher: Fetcher, first: Fetched) -> list[str]:
    """Follow `rel="next"` archived-feed links."""
    import feedparser

    entries: list[str] = []
    current = first
    for _ in range(MAX_FEED_PAGES):
        parsed = feedparser.parse(current.body)
        next_url = None
        for link in (parsed.get("feed") or {}).get("links") or []:
            if link.get("rel") == "next" and link.get("href"):
                next_url = urljoin(current.url, link["href"])
                break
        if not next_url:
            break
        result = await fetcher.get(next_url)
        if not result.ok:
            break
        page = parse_feed(result.body, next_url)
        if not page.entries:
            break
        entries += page.entries
        current = result
    return entries


async def probe_candidates(fetcher: Fetcher, origin: str, paths: tuple[str, ...]) -> list[str]:
    """Which of the usual paths actually exist on this origin."""
    found: list[str] = []
    for path in paths:
        result = await fetcher.head_or_get(urljoin(origin, path))
        if result.ok:
            found.append(str(result.url))
    return found


# ── helpers ──────────────────────────────────────────────────────────────


def _normalize(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _with_query(url: str, query: str) -> str:
    parts = urlsplit(url)
    merged = f"{parts.query}&{query}" if parts.query else query
    return urlunsplit((parts.scheme, parts.netloc, parts.path, merged, ""))


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
