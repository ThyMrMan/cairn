"""Watching a site for new content (docs/08).

A feed is polled, its entries are deduplicated against what has been seen
before, and anything new becomes a pending item that a capture picks up. A
sitemap watcher is the same machinery with a different parser — which is the
point of putting them in one module rather than two.

Three things here are load-bearing and easy to get wrong.

**Parsing is `feedparser`'s job, not ours.** The ArchiveBox evaluation's third
issue was a regex reader built around RSS 2.0's `<link>text</link>` that could
not see Atom's `<link href="...">` — Blogger's default output — so an Atom feed
appeared to contain nothing and the documented workaround was forcing
`&alt=rss`. feedparser reads RSS 0.9x/1.0/2.0, Atom 0.3/1.0, CDF and JSON Feed
plus a great deal of malformed real XML, which turns a whole class of bug into
a non-issue instead of a workaround.

**Absence means something different in a feed and in a sitemap.** A feed
carries the most recent N entries and nothing else, so an entry falling out of
it is the normal course of events and says nothing at all. A sitemap is meant
to be the complete list, so a URL vanishing from one is a real event — the
"a post you archived no longer exists upstream" notification. Disappearance is
therefore only ever inferred from sitemaps. Inferring it from feeds would fire
that alert on every poll of every healthy blog.

**The dedup key is the guid, and the canonical URL is the backstop.** Some
platforms regenerate guids whenever a post is edited; keyed on the guid alone,
one editorial pass re-captures the entire archive. Both are stored — canonical
for recognition, raw for fetching, because the canonical form drops query
parameters the site may well need.

The split between fetching and storing is the same one discovery uses: the
network half is async and knows nothing about the database, the database half
is synchronous and does no I/O. That is what lets the scheduler run polls
concurrently without a session crossing a thread.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.db.models import Feed, FeedItem, FeedPoll, Site
from cairn.db.types import utcnow
from cairn.discovery.fetch import Fetched, Fetcher
from cairn.logging import get_logger

log = get_logger(__name__)

MIN_INTERVAL_MIN = 5
DEFAULT_INTERVAL_MIN = 360
DEFAULT_SITEMAP_INTERVAL_MIN = 1440
# A dead feed must not poll every ten minutes forever, and a feed that is
# merely being rate-limited must not be given up on.
MAX_BACKOFF_MIN = 24 * 60
FAILURES_BEFORE_DISABLE = 10
# ±10%, so twenty feeds added in one sitting do not all fire in the same second
# for the rest of the installation's life.
JITTER_FRACTION = 0.10

# How many polls to keep per feed. The history is the feature; an unbounded
# history is a table that grows by 4 rows an hour per feed forever.
POLL_HISTORY_LIMIT = 200

# Entry titles are shown; bodies are not stored at all. A feed is not an
# archive of itself — the capture is.
MAX_TITLE_CHARS = 500

FEED_KINDS = ("auto", "rss", "atom", "json", "sitemap")

# Dropped before comparing two URLs. Deliberately short: every one of these is
# provably not part of a document's identity, and a parameter wrongly dropped
# merges two different posts into one archive entry.
TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
        # Blogger serves a mobile variant of every page at ?m=1. It is the same
        # post; treating the two as different doubles the archive.
        "m",
    }
)


class FeedError(ValueError):
    """A feed could not be added, parsed or polled."""


# ── canonicalization ─────────────────────────────────────────────────────


def canonical_url(url: str) -> str:
    """The form two URLs are compared in.

    Never fetched — `FeedItem.url` keeps the original for that. Dropping a
    parameter that turns out to matter would be a request for a different page,
    and the point here is only to recognise a page already seen.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not scheme or not host:
        return url.strip()

    netloc = host
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not (key.lower() in TRACKING_PARAMS or key.lower().startswith("utm_"))
    ]
    # Sorted, so ?a=1&b=2 and ?b=2&a=1 are one URL. A fragment is never sent to
    # the server and cannot identify a different document.
    query = urlencode(sorted(kept))
    return urlunsplit((scheme, netloc, path, query, ""))


# ── parsing ──────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Entry:
    """One item, in the shape the database stores."""

    guid: str
    url: str
    canonical: str
    title: str | None = None
    published: datetime | None = None
    updated: datetime | None = None


@dataclass(slots=True)
class ParsedFeed:
    kind: str = "unknown"
    title: str | None = None
    entries: list[Entry] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def parse(body: bytes, url: str) -> ParsedFeed:
    """Read a feed body into entries.

    A feed that parses to zero entries is not an error — a brand new blog has
    one — but a feed that parses to zero entries *and* raised is. feedparser
    sets `bozo` for anything it had to recover from, including things it
    recovered from perfectly well, so the flag alone is not a verdict.
    """
    import feedparser

    parsed = feedparser.parse(body)
    meta = parsed.get("feed") or {}
    result = ParsedFeed(kind=_kind_of(parsed))
    if hasattr(meta, "get"):
        title = meta.get("title")
        result.title = str(title)[:255] if title else None

    for raw in parsed.get("entries") or []:
        entry = _entry_from(raw)
        if entry is not None:
            result.entries.append(entry)

    if result.entries:
        return result
    # An empty feed is not an error — a brand new blog has one, and it has a
    # version. A document with neither entries nor a recognised version is not
    # a feed at all, which is what an HTML error page served with 200 looks
    # like. `bozo` alone is not the test: feedparser sets it for anything it
    # had to recover from, including things it recovered from perfectly well.
    if parsed.get("bozo"):
        result.error = str(parsed.get("bozo_exception") or "not a parseable feed")[:300]
    elif result.kind in ("unknown", ""):
        result.error = "parsed, but it is not a feed in any format feedparser recognises"
    return result


def _kind_of(parsed: Any) -> str:
    """rss / atom / json, from whatever feedparser called the version."""
    version = str(parsed.get("version") or "").lower()
    if version.startswith("atom"):
        return "atom"
    if version.startswith("rss") or version.startswith("cdf"):
        return "rss"
    if version.startswith("json"):
        return "json"
    return version or "unknown"


def _entry_from(raw: Any) -> Entry | None:
    link = raw.get("link")
    if not link:
        for candidate in raw.get("links") or []:
            if candidate.get("rel") in (None, "alternate") and candidate.get("href"):
                link = candidate["href"]
                break
    if not link:
        return None

    link = str(link).strip()
    canonical = canonical_url(link)
    # `id` is the Atom/RSS guid. Falling back to the canonical URL rather than
    # the raw one keeps the key stable when the same post is linked once with
    # tracking parameters and once without.
    guid = str(raw.get("id") or "").strip() or canonical
    title = raw.get("title")
    return Entry(
        guid=guid[:2048],
        url=link[:2048],
        canonical=canonical[:2048],
        title=str(title)[:MAX_TITLE_CHARS] if title else None,
        published=_stamp(raw.get("published_parsed")),
        updated=_stamp(raw.get("updated_parsed")),
    )


def _stamp(value: Any) -> datetime | None:
    """feedparser's struct_time (always UTC) as an aware datetime."""
    import calendar
    from datetime import UTC

    if value is None:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(value), tz=UTC)
    except (TypeError, ValueError, OverflowError):  # pragma: no cover — absurd dates
        return None


# ── fetching ─────────────────────────────────────────────────────────────


@dataclass(slots=True)
class FeedState:
    """Everything the network half needs, copied out of the row.

    A plain snapshot rather than the ORM object, so the fetch can run off the
    event loop's thread without a Session travelling with it.
    """

    id: int
    url: str
    kind: str
    etag: str | None = None
    last_modified: str | None = None
    user_agent: str | None = None


@dataclass(slots=True)
class FetchResult:
    status: int = 0
    not_modified: bool = False
    parsed: ParsedFeed = field(default_factory=ParsedFeed)
    etag: str | None = None
    last_modified: str | None = None
    duration_ms: int = 0
    error: str | None = None
    # Sitemaps only: whether the whole document set was read successfully. A
    # partial walk must not be diffed, or every sitemap that failed to paginate
    # reports its own site as having vanished.
    complete: bool = True

    @property
    def ok(self) -> bool:
        return self.error is None


async def fetch(state: FeedState, *, fetcher: Fetcher) -> FetchResult:
    """One conditional poll. Network only — nothing here touches the database."""
    started = time.monotonic()
    headers: dict[str, str] = {}
    if state.etag:
        headers["If-None-Match"] = state.etag
    if state.last_modified:
        headers["If-Modified-Since"] = state.last_modified

    if state.kind == "sitemap":
        result = await _fetch_sitemap(state, fetcher, headers)
    else:
        result = await _fetch_feed(state, fetcher, headers)
    result.duration_ms = int((time.monotonic() - started) * 1000)
    return result


async def _fetch_feed(state: FeedState, fetcher: Fetcher, headers: dict[str, str]) -> FetchResult:
    got = await fetcher.get(state.url, headers=headers)
    result = _conditional(got)
    if result is not None:
        return result

    # Off the loop: an 8 MB feed is a second of XML parsing, and blocking here
    # would stall every other poll and every SSE heartbeat behind it.
    parsed = await asyncio.to_thread(parse, got.body, state.url)
    return FetchResult(
        status=got.status,
        parsed=parsed,
        etag=got.headers.get("etag"),
        last_modified=got.headers.get("last-modified"),
        error=parsed.error,
    )


async def _fetch_sitemap(
    state: FeedState, fetcher: Fetcher, headers: dict[str, str]
) -> FetchResult:
    from cairn.discovery.sources import collect_sitemaps

    got = await fetcher.get(state.url, headers=headers)
    result = _conditional(got)
    if result is not None:
        return result

    # The walk re-fetches the root, so a changed sitemap costs one duplicate
    # request. Worth it: the conditional GET above is what makes the common
    # case — an unchanged sitemap, daily, forever — cost nothing at all, and
    # threading an already-fetched document into the walker would complicate
    # the one function that has to handle indexes, pagination and cycles.
    source = await collect_sitemaps(fetcher, [state.url])
    entries = [
        Entry(guid=canonical_url(url), url=url, canonical=canonical_url(url)) for url in source.urls
    ]
    return FetchResult(
        status=got.status,
        parsed=ParsedFeed(kind="sitemap", entries=entries),
        etag=got.headers.get("etag"),
        last_modified=got.headers.get("last-modified"),
        error=None if entries else (source.errors[0] if source.errors else "sitemap has no URLs"),
        # Errors part-way through a walk mean some documents were not read, so
        # the URL set is not the site's full set and must not drive removals.
        complete=not source.errors,
    )


def _conditional(got: Fetched) -> FetchResult | None:
    """Turn a 304 or a failed request into a result; None means carry on."""
    if got.not_modified:
        return FetchResult(status=got.status, not_modified=True)
    if got.error:
        return FetchResult(status=got.status, error=got.error)
    if not got.ok:
        return FetchResult(status=got.status, error=f"HTTP {got.status}")
    return None


# ── applying a poll to the database ──────────────────────────────────────


@dataclass(slots=True)
class PollOutcome:
    """What one poll found.

    `new_items` is "items this poll left pending", which is what a capture
    needs — so it includes an already-captured post that was edited on a feed
    with `recapture_on_update` set, not only entries never seen before.
    """

    status: int = 0
    entries_seen: int = 0
    new_items: list[int] = field(default_factory=list)
    gone_items: list[int] = field(default_factory=list)
    action: str = ""
    error: str | None = None
    disabled: bool = False
    # The first successful poll of a feed, which records what already exists
    # without capturing any of it. See `_merge`.
    baseline: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


def apply(session: Session, feed: Feed, result: FetchResult) -> PollOutcome:
    """Record what a poll found. Database only — nothing here does I/O.

    Returns the item ids that are new, which is what the caller turns into a
    capture. It does not enqueue anything itself: whether new content should be
    captured right now is a scheduling question (quiet hours, per-host
    serialization), and this function is also what a manual "poll now" runs.
    """
    now = utcnow()
    # Read before it is overwritten below. A feed that has never succeeded is
    # being baselined, not polled, and everything downstream depends on that.
    first_poll = feed.last_success_at is None
    feed.last_polled_at = now
    feed.last_status = result.status or None

    if not result.ok:
        return _record_failure(session, feed, result, now)

    feed.consecutive_failures = 0
    feed.last_error = None
    feed.last_success_at = now
    if result.etag:
        feed.etag = result.etag[:255]
    if result.last_modified:
        feed.last_modified = result.last_modified[:64]
    feed.next_poll_at = next_due(feed, now=now)

    if result.not_modified:
        outcome = PollOutcome(status=result.status, action="not modified")
        _record(session, feed, outcome, result.duration_ms)
        return outcome

    # Read before `kind` can be rewritten below.
    is_sitemap = feed.kind == "sitemap"

    if result.parsed.title and not feed.title:
        feed.title = result.parsed.title
    if feed.kind == "auto" and result.parsed.kind not in ("unknown", ""):
        feed.kind = result.parsed.kind

    new_ids, seen_ids = _merge(session, feed, result.parsed.entries, now, baseline=first_poll)
    gone_ids = _mark_gone(session, feed, seen_ids, now) if (is_sitemap and result.complete) else []

    outcome = PollOutcome(
        status=result.status,
        entries_seen=len(result.parsed.entries),
        new_items=[] if first_poll else new_ids,
        gone_items=gone_ids,
        action=(
            f"baseline: {len(new_ids)} existing"
            if first_poll
            else _describe(len(new_ids), len(gone_ids))
        ),
        baseline=first_poll,
    )
    _record(session, feed, outcome, result.duration_ms)
    return outcome


def _merge(
    session: Session, feed: Feed, entries: list[Entry], now: datetime, *, baseline: bool = False
) -> tuple[list[int], set[int]]:
    """Insert what is new, touch what is not.

    Returns the ids left pending, and every id this poll saw — the second is
    what tells a sitemap watcher which of its items have *not* been seen.
    Deliberately a set of ids rather than a timestamp comparison: two polls
    inside one second would be indistinguishable by time, and the failure mode
    of getting that wrong is announcing that every archived URL has vanished.

    **The first poll of a feed is a baseline, not a backlog.** Every entry in a
    feed is new the first time it is read, so capturing them all would mean
    adding a watch to a blog and immediately re-fetching its entire archive one
    post at a time — the most expensive possible way to obtain what a single
    full capture already covers. So a first poll records everything as seen and
    captures none of it, and the watch starts meaning "new" from that moment.
    """
    if not entries:
        return [], set()

    by_guid = {
        item.guid: item
        for item in session.scalars(select(FeedItem).where(FeedItem.feed_id == feed.id)).all()
    }
    by_canonical = {item.canonical_url: item for item in by_guid.values() if item.canonical_url}

    new_ids: list[int] = []
    seen_ids: set[int] = set()
    for entry in entries:
        # The guid first, then the canonical URL. The second lookup is what
        # stops a platform that regenerates guids on every edit from
        # re-capturing its whole archive.
        existing = by_guid.get(entry.guid) or by_canonical.get(entry.canonical)
        if existing is not None:
            existing.last_seen_at = now
            existing.gone_at = None
            seen_ids.add(existing.id)
            known = existing.updated_at
            if entry.updated and (known is None or entry.updated > known):
                existing.updated_at = entry.updated
                if feed.recapture_on_update and existing.status == "captured":
                    existing.status = "pending"
                    new_ids.append(existing.id)
            continue

        item = FeedItem(
            feed_id=feed.id,
            guid=entry.guid,
            url=entry.url,
            canonical_url=entry.canonical,
            title=entry.title,
            published_at=entry.published,
            updated_at=entry.updated,
            first_seen_at=now,
            last_seen_at=now,
            status="skipped" if baseline else "pending",
        )
        session.add(item)
        session.flush()
        by_guid[entry.guid] = item
        by_canonical[entry.canonical] = item
        seen_ids.add(item.id)
        new_ids.append(item.id)

    return new_ids, seen_ids


def _mark_gone(session: Session, feed: Feed, seen: set[int], now: datetime) -> list[int]:
    """Items this sitemap no longer lists.

    Only ever called for sitemaps, and only for a complete read — see the
    module docstring for both halves of why.
    """
    stale = session.scalars(
        select(FeedItem).where(
            FeedItem.feed_id == feed.id,
            FeedItem.id.notin_(seen or {0}),
            FeedItem.gone_at.is_(None),
        )
    ).all()
    for item in stale:
        item.gone_at = now
    return [item.id for item in stale]


def _record_failure(
    session: Session, feed: Feed, result: FetchResult, now: datetime
) -> PollOutcome:
    feed.consecutive_failures += 1
    feed.last_error = (result.error or "poll failed")[:1000]
    feed.next_poll_at = next_due(feed, now=now)

    disabled = False
    if feed.consecutive_failures >= FAILURES_BEFORE_DISABLE:
        feed.enabled = False
        feed.disabled_reason = (
            f"Turned off after {feed.consecutive_failures} failed polls in a row. "
            f"The last error was: {feed.last_error}"
        )
        disabled = True

    outcome = PollOutcome(
        status=result.status,
        action="disabled" if disabled else "failed",
        error=feed.last_error,
        disabled=disabled,
    )
    _record(session, feed, outcome, result.duration_ms)
    return outcome


def _describe(new: int, gone: int) -> str:
    if not new and not gone:
        return "no change"
    parts = []
    if new:
        parts.append(f"{new} new")
    if gone:
        parts.append(f"{gone} gone")
    return ", ".join(parts)


def _record(session: Session, feed: Feed, outcome: PollOutcome, duration_ms: int) -> None:
    session.add(
        FeedPoll(
            feed_id=feed.id,
            ts=utcnow(),
            status=outcome.status,
            duration_ms=duration_ms,
            entries_seen=outcome.entries_seen,
            new_items=len(outcome.new_items),
            gone_items=len(outcome.gone_items),
            action=outcome.action[:32],
            error=outcome.error,
        )
    )
    session.flush()
    prune_history(session, feed.id)


def prune_history(session: Session, feed_id: int) -> int:
    """Keep the most recent polls and drop the rest."""
    keep = session.scalars(
        select(FeedPoll.id)
        .where(FeedPoll.feed_id == feed_id)
        .order_by(FeedPoll.ts.desc(), FeedPoll.id.desc())
        .limit(POLL_HISTORY_LIMIT)
    ).all()
    if len(keep) < POLL_HISTORY_LIMIT:
        return 0
    stale = session.scalars(
        select(FeedPoll).where(FeedPoll.feed_id == feed_id, FeedPoll.id.notin_(keep))
    ).all()
    for row in stale:
        session.delete(row)
    # Flushed here rather than left pending. A delete that only lands on the
    # *next* write leaves the table one row over the limit forever, and the
    # last poll before a shutdown never trims at all.
    session.flush()
    return len(stale)


# ── scheduling ───────────────────────────────────────────────────────────


def next_due(feed: Feed, *, now: datetime | None = None) -> datetime:
    """When this feed should next be polled.

    Backoff doubles per consecutive failure, capped at a day, so a blog that
    went away stops costing a request every ten minutes without ever being
    given up on entirely.

    Jitter is applied to every poll, not only the first. Twenty feeds added in
    one sitting share an interval, and without per-poll jitter they converge
    back onto the same second within a couple of cycles.
    """
    now = now or utcnow()
    minutes = max(feed.interval_min, MIN_INTERVAL_MIN)
    if feed.consecutive_failures:
        minutes = min(minutes * (2**feed.consecutive_failures), MAX_BACKOFF_MIN)
    spread = minutes * JITTER_FRACTION
    return now + timedelta(minutes=minutes + random.uniform(-spread, spread))  # noqa: S311


def due_feeds(session: Session, *, now: datetime | None = None, limit: int = 50) -> list[Feed]:
    """Every enabled feed whose time has come.

    The whole scheduler is this query. Nothing persists a fire time that could
    be missed, so a container stopped for a week comes back and polls each
    overdue feed exactly once — rather than either skipping them or firing the
    entire backlog at once, which are the two things a job-store scheduler does
    depending on how it is configured (docs/08).
    """
    now = now or utcnow()
    return list(
        session.scalars(
            select(Feed)
            .join(Site, Site.id == Feed.site_id)
            .where(
                Feed.enabled.is_(True),
                Site.deleted_at.is_(None),
                (Feed.next_poll_at.is_(None)) | (Feed.next_poll_at <= now),
            )
            .order_by(Feed.next_poll_at.is_(None).desc(), Feed.next_poll_at.asc())
            .limit(limit)
        ).all()
    )


# ── finding feeds, and testing one before it is saved ────────────────────

# Comment feeds, by convention. Blogger and WordPress both publish one beside
# the posts feed, and it is almost always noise: hundreds of entries whose URLs
# are fragments of pages the posts feed already covers.
COMMENT_MARKERS = ("/comments/", "/comments", "comments/feed", "/feeds/comments")

# Tried on top of whatever the platform preset suggests. Ordered so the
# likeliest wins the "posts feed" slot when several exist.
GENERIC_FEED_PATHS = (
    "/feed",
    "/feed.xml",
    "/rss",
    "/rss.xml",
    "/atom.xml",
    "/index.xml",
    "/feeds/posts/default",
)


@dataclass(slots=True)
class FeedCandidate:
    """A feed that could be watched, and what it looks like right now.

    Everything the add dialog shows comes from here, which is deliberate: a
    feed whose entries fall outside the site's scope is a real and thoroughly
    confusing failure — it polls forever, finds new posts every time, and
    archives none of them — so it is caught before the feed is saved rather
    than diagnosed later from a poll history.
    """

    url: str
    kind: str = "unknown"
    title: str | None = None
    entry_count: int = 0
    recent_titles: list[str] = field(default_factory=list)
    is_comments: bool = False
    in_scope: int = 0
    out_of_scope: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "kind": self.kind,
            "title": self.title,
            "entry_count": self.entry_count,
            "recent_titles": self.recent_titles,
            "is_comments": self.is_comments,
            "in_scope": self.in_scope,
            "out_of_scope": self.out_of_scope,
            "error": self.error,
            "ok": self.ok,
        }


def is_comment_feed(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return any(marker in path for marker in COMMENT_MARKERS)


async def probe(
    url: str, *, fetcher: Fetcher, kind: str = "auto", scope: Any = None
) -> FeedCandidate:
    """Fetch and parse one candidate, and check it against a scope.

    `scope` is a `services.scope.Scope` or None. When given, every entry URL is
    tested against the hosts the capture would actually crawl.
    """
    state = FeedState(id=0, url=url, kind=kind)
    result = await fetch(state, fetcher=fetcher)

    candidate = FeedCandidate(url=url, is_comments=is_comment_feed(url))
    if not result.ok:
        candidate.error = result.error
        return candidate
    if result.not_modified:  # pragma: no cover — a probe sends no validators
        candidate.error = "the server said nothing had changed, which a first fetch cannot mean"
        return candidate

    candidate.kind = result.parsed.kind
    candidate.title = result.parsed.title
    candidate.entry_count = len(result.parsed.entries)
    candidate.recent_titles = [e.title for e in result.parsed.entries[:3] if e.title]
    if not result.parsed.entries:
        candidate.error = "parsed, but it contains no entries"
        return candidate

    if scope is not None:
        inside, outside = split_by_scope([e.url for e in result.parsed.entries], scope)
        candidate.in_scope = len(inside)
        candidate.out_of_scope = outside[:5]
    else:
        candidate.in_scope = candidate.entry_count
    return candidate


def split_by_scope(urls: list[str], scope: Any) -> tuple[list[str], list[str]]:
    """Which of these URLs a capture with this scope would actually fetch."""
    from cairn.services.discovery_service import compiled_rejects

    crawlable = {rule.host for rule in scope.hosts if rule.crawl_pages}
    rejects = compiled_rejects(scope)
    inside: list[str] = []
    outside: list[str] = []
    for url in urls:
        host = (urlsplit(url).hostname or "").lower()
        if host in crawlable and not any(pattern.search(url) for pattern in rejects):
            inside.append(url)
        else:
            outside.append(url)
    return inside, outside


async def discover(
    seed_url: str, *, fetcher: Fetcher, preset: Any = None, scope: Any = None
) -> list[FeedCandidate]:
    """Every feed and sitemap worth offering for this site.

    Three sources, per docs/08: the page's own `<link rel="alternate">`, the
    platform's conventional paths, and the sitemaps `robots.txt` declares. The
    result is a list to choose from — nothing here writes a row.
    """
    from urllib.parse import urljoin

    from cairn.discovery import sources
    from cairn.services import htmlrefs

    origin = _origin_of(seed_url)
    candidates: list[str] = []

    page = await fetcher.get(seed_url)
    if page.ok and page.is_html:
        candidates += htmlrefs.parse_page(page.body, str(page.url), want_links=False).feeds

    paths = tuple(getattr(preset, "feed_paths", ()) or ()) + GENERIC_FEED_PATHS
    candidates += [urljoin(origin, path) for path in paths]

    seen: set[str] = set()
    found: list[FeedCandidate] = []
    for url in candidates:
        key = canonical_url(url)
        if key in seen:
            continue
        seen.add(key)
        candidate = await probe(url, fetcher=fetcher, scope=scope)
        if candidate.ok:
            found.append(candidate)

    # Sitemaps are offered as watchers rather than probed as feeds: a sitemap
    # is not a feed and parsing one as such reports it as broken. Feeds are for
    # latency, sitemaps for completeness — a serious archive runs both.
    robots = await sources.fetch_robots(fetcher, origin)
    conventional = tuple(getattr(preset, "sitemap_paths", ()) or ()) or sources.SITEMAP_CANDIDATES
    sitemap_urls = list(robots.sitemaps)
    sitemap_urls += [urljoin(origin, path) for path in conventional]
    for url in sitemap_urls:
        key = canonical_url(url)
        if key in seen:
            continue
        seen.add(key)
        probed = await fetcher.head_or_get(url)
        if probed.ok:
            found.append(FeedCandidate(url=str(probed.url), kind="sitemap"))

    return found


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"


def default_interval(kind: str) -> int:
    """A sitemap is checked daily; a feed six-hourly (docs/08)."""
    return DEFAULT_SITEMAP_INTERVAL_MIN if kind == "sitemap" else DEFAULT_INTERVAL_MIN


def add_feed(
    session: Session,
    site: Site,
    *,
    url: str,
    kind: str = "auto",
    interval_min: int | None = None,
    enabled: bool = True,
    auto_capture: bool = True,
    title: str | None = None,
) -> Feed:
    """Attach a feed to a site, refusing a duplicate.

    A new feed is due immediately rather than one interval from now: somebody
    who has just added a feed wants to see it work, and waiting six hours to
    find out it was the wrong URL is the opposite of the observability this
    milestone is for.
    """
    cleaned = url.strip()
    parts = urlsplit(cleaned)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FeedError(f"not an absolute http(s) URL: {url!r}")
    if kind not in FEED_KINDS:
        raise FeedError(f"unknown feed kind {kind!r}")

    existing = session.scalar(
        select(Feed).where(Feed.site_id == site.id, Feed.url == cleaned).limit(1)
    )
    if existing is not None:
        raise FeedError("that feed is already attached to this site")

    feed = Feed(
        site_id=site.id,
        url=cleaned,
        kind=kind,
        title=title,
        enabled=enabled,
        auto_capture=auto_capture,
        interval_min=max(interval_min or default_interval(kind), MIN_INTERVAL_MIN),
        next_poll_at=utcnow(),
    )
    session.add(feed)
    session.flush()
    return feed


# ── reads ────────────────────────────────────────────────────────────────


def pending_items(session: Session, feed_id: int, *, limit: int = 500) -> list[FeedItem]:
    return list(
        session.scalars(
            select(FeedItem)
            .where(FeedItem.feed_id == feed_id, FeedItem.status == "pending")
            .order_by(FeedItem.published_at.desc().nulls_last(), FeedItem.id.asc())
            .limit(limit)
        ).all()
    )


def counts_for(session: Session, feed_id: int) -> dict[str, int]:
    from sqlalchemy import func

    rows = session.execute(
        select(FeedItem.status, func.count(FeedItem.id))
        .where(FeedItem.feed_id == feed_id)
        .group_by(FeedItem.status)
    ).all()
    by_status = {str(status): int(count) for status, count in rows}
    gone = (
        session.scalar(
            select(func.count(FeedItem.id)).where(
                FeedItem.feed_id == feed_id, FeedItem.gone_at.isnot(None)
            )
        )
        or 0
    )
    return {
        "seen": sum(by_status.values()),
        "pending": by_status.get("pending", 0),
        "captured": by_status.get("captured", 0),
        "failed": by_status.get("failed", 0),
        "skipped": by_status.get("skipped", 0),
        "gone": int(gone),
    }


def history(session: Session, feed_id: int, *, limit: int = 50) -> list[FeedPoll]:
    return list(
        session.scalars(
            select(FeedPoll)
            .where(FeedPoll.feed_id == feed_id)
            .order_by(FeedPoll.ts.desc(), FeedPoll.id.desc())
            .limit(limit)
        ).all()
    )
