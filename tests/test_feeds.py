"""Feeds, watchers, scheduling and notifications — M6's moving parts.

Everything here runs without a network except where a fixture server is asked
for, and nothing here needs wget or Chromium; the end-to-end proof lives in
`test_feeds_e2e.py`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Feed, FeedItem, FeedPoll, Job, Site
from cairn.db.types import utcnow
from cairn.services import feeds, notify, scheduler, settings_store
from tests.conftest import XHR, Blog


def _count(db: Session, feed_id: int, **where: object) -> int:
    query = select(func.count(FeedItem.id)).where(FeedItem.feed_id == feed_id)
    for column, value in where.items():
        query = query.where(getattr(FeedItem, column) == value)
    return int(db.scalar(query) or 0)


def _local(hour: int) -> datetime:
    return datetime.now().astimezone().replace(hour=hour, minute=30)


ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>A Blog</title>
  <entry>
    <id>tag:blogger.com,1999:blog-1.post-1</id>
    <title>First post</title>
    <link rel="alternate" type="text/html" href="https://blog.example/2026/08/first.html"/>
    <published>2026-08-01T10:00:00Z</published>
    <updated>2026-08-01T10:00:00Z</updated>
  </entry>
</feed>"""

# The form the ArchiveBox evaluation's parser could read, kept so the two are
# tested side by side rather than one standing in for the other.
RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>A Blog</title>
  <item>
    <guid>https://blog.example/2026/08/first.html</guid>
    <title>First post</title>
    <link>https://blog.example/2026/08/first.html</link>
    <pubDate>Sat, 01 Aug 2026 10:00:00 GMT</pubDate>
  </item>
</channel></rss>"""


# ── canonicalization ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Case and default ports carry no meaning.
        ("https://Example.COM:443/post.html", "https://example.com/post.html"),
        ("http://example.com:80/post.html", "http://example.com/post.html"),
        # A non-default port does.
        ("http://example.com:8080/p", "http://example.com:8080/p"),
        # A trailing slash on a path, but never on the root.
        ("https://example.com/post/", "https://example.com/post"),
        ("https://example.com/", "https://example.com/"),
        # Tracking parameters, and Blogger's mobile duplicate.
        ("https://x.test/p?utm_source=rss&utm_medium=feed", "https://x.test/p"),
        ("https://x.test/p?fbclid=abc", "https://x.test/p"),
        ("https://x.test/p?m=1", "https://x.test/p"),
        # Parameters that are not tracking survive, in a stable order.
        ("https://x.test/p?b=2&a=1", "https://x.test/p?a=1&b=2"),
        ("https://x.test/p?id=7&utm_campaign=q", "https://x.test/p?id=7"),
        # A fragment is never sent to a server.
        ("https://x.test/p#section", "https://x.test/p"),
    ],
)
def test_urls_that_mean_the_same_page_canonicalize_together(raw: str, expected: str) -> None:
    assert feeds.canonical_url(raw) == expected


def test_a_url_that_is_not_a_url_survives_canonicalization() -> None:
    """Fed a relative link by a malformed feed, the answer must not be a crash."""
    assert feeds.canonical_url("/just/a/path") == "/just/a/path"


# ── parsing ──────────────────────────────────────────────────────────────


def test_atom_link_href_is_read() -> None:
    """The ArchiveBox gap: a parser looking for `<link>` *text* finds nothing in
    Atom, which is Blogger's default output, so the feed appears to be empty."""
    parsed = feeds.parse(ATOM, "https://blog.example/feeds/posts/default")

    assert parsed.kind == "atom"
    assert parsed.title == "A Blog"
    assert [e.url for e in parsed.entries] == ["https://blog.example/2026/08/first.html"]
    assert parsed.entries[0].guid == "tag:blogger.com,1999:blog-1.post-1"
    assert parsed.entries[0].published is not None


def test_rss_is_read_the_same_way() -> None:
    parsed = feeds.parse(RSS, "https://blog.example/rss")

    assert parsed.kind == "rss"
    assert [e.title for e in parsed.entries] == ["First post"]


def test_something_that_is_not_a_feed_says_so() -> None:
    parsed = feeds.parse(b"<html><body><h1>Not a feed</h1></body></html>", "https://x.test/")

    assert not parsed.entries
    assert parsed.error


def test_an_entry_with_no_guid_falls_back_to_its_canonical_url() -> None:
    body = (
        b'<?xml version="1.0"?><rss version="2.0"><channel>'
        b"<item><link>https://x.test/p?utm_source=rss</link></item>"
        b"</channel></rss>"
    )
    parsed = feeds.parse(body, "https://x.test/rss")

    assert parsed.entries[0].guid == "https://x.test/p"
    assert parsed.entries[0].url == "https://x.test/p?utm_source=rss", (
        "the raw URL is what gets fetched; only the key is canonical"
    )


# ── merging, deduplication and the baseline ──────────────────────────────


def _feed_for(db: Session, site: Site, *, kind: str = "atom", **kwargs: object) -> Feed:
    feed = Feed(site_id=site.id, url="https://blog.example/feed", kind=kind, **kwargs)
    db.add(feed)
    db.flush()
    return feed


def _result(entries: list[feeds.Entry], **kwargs: object) -> feeds.FetchResult:
    return feeds.FetchResult(
        status=200, parsed=feeds.ParsedFeed(kind="atom", entries=entries), **kwargs
    )


def _entry(slug: str, *, guid: str | None = None, updated: object = None) -> feeds.Entry:
    url = f"https://blog.example/{slug}.html"
    return feeds.Entry(
        guid=guid or f"urn:{slug}",
        url=url,
        canonical=feeds.canonical_url(url),
        title=slug,
        updated=updated,  # type: ignore[arg-type]
    )


@pytest.fixture
def site(authed: TestClient, db: Session) -> Site:
    res = authed.post(
        "/api/sites", json={"seed_url": "https://blog.example/", "title": "Blog"}, headers=XHR
    )
    assert res.status_code == 201, res.text
    return db.get(Site, res.json()["id"])  # type: ignore[return-value]


def test_the_first_poll_is_a_baseline_not_a_backlog(db: Session, site: Site) -> None:
    """Every entry in a feed is new the first time it is read.

    Capturing them would mean adding a watch to a blog and immediately
    re-fetching its whole archive one post at a time — the most expensive
    possible way to get what one full capture already covers.
    """
    feed = _feed_for(db, site)

    outcome = feeds.apply(db, feed, _result([_entry("a"), _entry("b"), _entry("c")]))

    assert outcome.baseline
    assert outcome.new_items == []
    assert "baseline" in outcome.action
    assert _count(db, feed.id) == 3
    assert not feeds.pending_items(db, feed.id), "a baseline must leave nothing to capture"


def test_a_post_published_after_the_baseline_is_new(db: Session, site: Site) -> None:
    feed = _feed_for(db, site)
    feeds.apply(db, feed, _result([_entry("a"), _entry("b")]))

    outcome = feeds.apply(db, feed, _result([_entry("c"), _entry("a"), _entry("b")]))

    assert not outcome.baseline
    assert len(outcome.new_items) == 1
    assert [item.url for item in feeds.pending_items(db, feed.id)] == [
        "https://blog.example/c.html"
    ]


def test_a_regenerated_guid_does_not_recapture_the_archive(db: Session, site: Site) -> None:
    """Some platforms mint a fresh guid whenever a post is edited.

    Keyed on the guid alone, one editorial pass re-captures everything. The
    canonical URL is the backstop that stops it.
    """
    feed = _feed_for(db, site)
    feeds.apply(db, feed, _result([_entry("a"), _entry("b")]))

    outcome = feeds.apply(
        db,
        feed,
        _result([_entry("a", guid="urn:regenerated-1"), _entry("b", guid="urn:regenerated-2")]),
    )

    assert outcome.new_items == []
    assert _count(db, feed.id) == 2


def test_an_edited_post_is_recaptured_only_when_asked(db: Session, site: Site) -> None:
    """Default off: most feeds touch `updated` for trivial reasons."""
    quiet = _feed_for(db, site)
    feeds.apply(db, quiet, _result([_entry("a")]))
    for item in db.scalars(select(FeedItem).where(FeedItem.feed_id == quiet.id)).all():
        item.status = "captured"
    db.flush()

    outcome = feeds.apply(db, quiet, _result([_entry("a", updated=utcnow())]))
    assert outcome.new_items == []

    quiet.recapture_on_update = True
    outcome = feeds.apply(db, quiet, _result([_entry("a", updated=utcnow() + timedelta(hours=1))]))
    assert len(outcome.new_items) == 1


def test_a_not_modified_response_costs_nothing(db: Session, site: Site) -> None:
    feed = _feed_for(db, site)
    feeds.apply(db, feed, _result([_entry("a")]))
    before = _count(db, feed.id)

    outcome = feeds.apply(db, feed, feeds.FetchResult(status=304, not_modified=True))

    assert outcome.action == "not modified"
    assert outcome.status == 304
    assert _count(db, feed.id) == before


# ── the sitemap watcher ──────────────────────────────────────────────────


def test_a_url_missing_from_a_sitemap_has_disappeared(db: Session, site: Site) -> None:
    """The notification worth having: a post you archived is gone upstream."""
    watcher = _feed_for(db, site, kind="sitemap")
    feeds.apply(db, watcher, _result([_entry("a"), _entry("b")]))

    outcome = feeds.apply(db, watcher, _result([_entry("a")]))

    assert len(outcome.gone_items) == 1
    gone = db.scalars(select(FeedItem).where(FeedItem.gone_at.isnot(None))).one()
    assert gone.url.endswith("/b.html")


def test_a_partial_sitemap_read_never_reports_a_disappearance(db: Session, site: Site) -> None:
    """A walk that failed part-way has not seen the full URL set, so its
    absences are its own — not the site's."""
    watcher = _feed_for(db, site, kind="sitemap")
    feeds.apply(db, watcher, _result([_entry("a"), _entry("b")]))

    outcome = feeds.apply(db, watcher, _result([_entry("a")], complete=False))

    assert outcome.gone_items == []


def test_an_entry_leaving_a_feed_is_not_a_disappearance(db: Session, site: Site) -> None:
    """A feed carries the most recent N entries. Older ones falling off the end
    is the normal course of events, and treating it as removal would fire the
    alert on every poll of every healthy blog."""
    feed = _feed_for(db, site, kind="atom")
    feeds.apply(db, feed, _result([_entry("a"), _entry("b")]))

    outcome = feeds.apply(db, feed, _result([_entry("b")]))

    assert outcome.gone_items == []
    assert db.scalar(select(func.count(FeedItem.id)).where(FeedItem.gone_at.isnot(None))) == 0


# ── failure, backoff and giving up ───────────────────────────────────────


def test_repeated_failure_backs_off_and_eventually_stops(db: Session, site: Site) -> None:
    feed = _feed_for(db, site, interval_min=60)
    failure = feeds.FetchResult(status=500, error="HTTP 500")

    gaps = []
    for _ in range(feeds.FAILURES_BEFORE_DISABLE):
        before = utcnow()
        outcome = feeds.apply(db, feed, failure)
        assert feed.next_poll_at is not None
        gaps.append((feed.next_poll_at - before).total_seconds() / 60)

    assert gaps[1] > gaps[0], "the gap must widen"
    assert max(gaps) <= feeds.MAX_BACKOFF_MIN * 1.1, "and must stay capped at a day"
    assert outcome.disabled
    assert not feed.enabled
    assert feed.disabled_reason and "10" in feed.disabled_reason


def test_one_success_clears_the_backoff(db: Session, site: Site) -> None:
    feed = _feed_for(db, site, interval_min=60)
    feeds.apply(db, feed, feeds.FetchResult(status=500, error="HTTP 500"))
    feeds.apply(db, feed, feeds.FetchResult(status=500, error="HTTP 500"))
    assert feed.consecutive_failures == 2

    feeds.apply(db, feed, _result([_entry("a")]))

    assert feed.consecutive_failures == 0
    assert feed.last_error is None


def test_every_poll_is_recorded_whatever_happened(db: Session, site: Site) -> None:
    """The scheduler's whole claim to being trustworthy."""
    feed = _feed_for(db, site)
    feeds.apply(db, feed, _result([_entry("a")]))
    feeds.apply(db, feed, feeds.FetchResult(status=304, not_modified=True))
    feeds.apply(db, feed, feeds.FetchResult(status=0, error="connection refused"))

    rows = feeds.history(db, feed.id)
    assert [row.action for row in rows] == ["failed", "not modified", "baseline: 1 existing"]
    assert rows[0].error == "connection refused"


def test_poll_history_is_bounded(db: Session, site: Site) -> None:
    feed = _feed_for(db, site)
    for _ in range(feeds.POLL_HISTORY_LIMIT + 25):
        feeds.apply(db, feed, feeds.FetchResult(status=304, not_modified=True))

    kept = db.scalar(select(func.count(FeedPoll.id)).where(FeedPoll.feed_id == feed.id))
    assert kept == feeds.POLL_HISTORY_LIMIT


# ── scheduling ───────────────────────────────────────────────────────────


def test_a_feed_is_due_when_its_time_has_passed(db: Session, site: Site) -> None:
    now = utcnow()
    overdue = _feed_for(db, site, next_poll_at=now - timedelta(minutes=1))
    later = Feed(
        site_id=site.id,
        url="https://blog.example/other",
        next_poll_at=now + timedelta(hours=1),
    )
    never = Feed(site_id=site.id, url="https://blog.example/third", next_poll_at=None)
    db.add_all([later, never])
    db.flush()

    due = {feed.id for feed in feeds.due_feeds(db, now=now)}

    assert due == {overdue.id, never.id}


def test_a_disabled_feed_is_never_due(db: Session, site: Site) -> None:
    _feed_for(db, site, enabled=False, next_poll_at=utcnow() - timedelta(days=1))

    assert feeds.due_feeds(db) == []


def test_a_deleted_sites_feeds_stop_being_due(db: Session, site: Site) -> None:
    _feed_for(db, site, next_poll_at=utcnow() - timedelta(days=1))
    site.deleted_at = utcnow()
    db.flush()

    assert feeds.due_feeds(db) == []


def test_the_next_poll_is_jittered(db: Session, site: Site) -> None:
    """Twenty feeds on one interval must not converge on the same second."""
    feed = _feed_for(db, site, interval_min=360)

    gaps = {round((feeds.next_due(feed) - utcnow()).total_seconds()) for _ in range(20)}

    assert len(gaps) > 15, "the spread should be real, not a rounding artefact"
    assert all(360 * 60 * 0.85 < gap < 360 * 60 * 1.15 for gap in gaps)


# ── quiet hours ──────────────────────────────────────────────────────────


def test_quiet_hours_are_off_by_default(db: Session) -> None:
    """docs/08 defaults them on. That would mean adding a feed, watching a post
    appear, and seeing nothing for eighteen hours with no explanation."""
    assert not scheduler.in_quiet_hours(db)


def test_the_window_says_when_work_may_run(db: Session) -> None:
    settings_store.put(
        db, scheduler.QUIET_HOURS_SETTING, {"enabled": True, "start": "01:00", "end": "07:00"}
    )

    assert not scheduler.in_quiet_hours(db, _local(3)), "inside the window, work may run"
    assert scheduler.in_quiet_hours(db, _local(20)), "outside it, work waits"


def test_a_window_that_crosses_midnight_still_works(db: Session) -> None:
    settings_store.put(
        db, scheduler.QUIET_HOURS_SETTING, {"enabled": True, "start": "22:00", "end": "06:00"}
    )

    assert not scheduler.in_quiet_hours(db, _local(23))
    assert not scheduler.in_quiet_hours(db, _local(2))
    assert scheduler.in_quiet_hours(db, _local(12))


# ── per-host serialization ───────────────────────────────────────────────


def test_two_jobs_against_one_host_never_run_together(
    client: TestClient, db: Session, settings: Settings
) -> None:
    """Not a scheduling preference — politeness, and therefore binding on a
    capture somebody started by hand as well as on a scheduled one."""
    supervisor = client.app.state.supervisor  # type: ignore[attr-defined]
    settings_store.put(db, "jobs.per_host_serial", True)

    from cairn.services import sites as site_service

    one = site_service.create_site(db, settings, seed_url="https://same.example/a")
    two = site_service.create_site(db, settings, seed_url="https://same.example/b")
    elsewhere = site_service.create_site(db, settings, seed_url="https://other.example/")
    for site_row in (one, two, elsewhere):
        supervisor.enqueue(db, job_type="capture", site_id=site_row.id, spec={"kind": "full"})
    db.commit()

    claimed = supervisor._claim(10)

    hosts = [db.get(Job, job_id).site_id for job_id in claimed]
    assert len(claimed) == 2, f"one of the two same-host jobs should have waited: {hosts}"
    assert elsewhere.id in hosts, "a busy host must not stall an unrelated site"


def test_the_rule_can_be_switched_off(client: TestClient, db: Session, settings: Settings) -> None:
    supervisor = client.app.state.supervisor  # type: ignore[attr-defined]
    settings_store.put(db, "jobs.per_host_serial", False)

    from cairn.services import sites as site_service

    for path in ("a", "b"):
        site_row = site_service.create_site(db, settings, seed_url=f"https://same.example/{path}")
        supervisor.enqueue(db, job_type="capture", site_id=site_row.id, spec={"kind": "full"})
    db.commit()

    assert len(supervisor._claim(10)) == 2


# ── notifications ────────────────────────────────────────────────────────


def test_a_webhook_receives_the_message(
    client: TestClient, db: Session, webhook: tuple[str, list[dict[str, object]]]
) -> None:
    url, received = webhook
    notify.set_targets(db, [{"url": url}])
    db.commit()

    async def go() -> int:
        return await notify.send(
            client.app.state.sessionmaker,  # type: ignore[attr-defined]
            notify.CAPTURE_FAILED,
            title="Capture failed: Blog",
            body="wget exited 8",
        )

    assert asyncio.run(go()) == 1
    assert len(received) == 1
    payload = received[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["title"] == "Capture failed: Blog"
    assert "wget exited 8" in str(payload["message"])


def test_an_event_switched_off_sends_nothing(
    client: TestClient, db: Session, webhook: tuple[str, list[dict[str, object]]]
) -> None:
    url, received = webhook
    notify.set_targets(db, [{"url": url}])
    settings_store.put(db, notify.event_setting(notify.ITEMS_CAPTURED), False)
    db.commit()

    sent = asyncio.run(
        notify.send(
            client.app.state.sessionmaker,  # type: ignore[attr-defined]
            notify.ITEMS_CAPTURED,
            title="two new posts",
        )
    )

    assert sent == 0
    assert received == []


def test_a_target_that_is_down_does_not_raise(client: TestClient, db: Session) -> None:
    """A capture that finished must not be reported as failed because a webhook
    was unreachable."""
    notify.set_targets(db, [{"url": "http://127.0.0.1:1/nothing-listens-here"}])
    db.commit()

    sent = asyncio.run(
        notify.send(
            client.app.state.sessionmaker,  # type: ignore[attr-defined]
            notify.CAPTURE_FAILED,
            title="anything",
        )
    )

    assert sent == 0


def test_an_https_url_is_a_webhook_unless_it_says_ntfy() -> None:
    """Guessing would send ntfy's header protocol to a webhook, which produces
    a 400 nobody can explain."""
    assert notify.Target(url="https://hooks.example/services/x").kind == "webhook"
    assert notify.Target(url="https://ntfy.sh/my-topic").kind == "ntfy"
    assert notify.Target(url="ntfy://ntfy.example/my-topic").kind == "ntfy"
    assert notify.Target(url="discord://id/token").kind == "apprise"


# ── the API ──────────────────────────────────────────────────────────────


def test_a_feed_can_be_added_tested_and_polled(authed: TestClient, blog: Blog) -> None:
    site_id = authed.post(
        "/api/sites", json={"seed_url": blog.base, "title": "Blog"}, headers=XHR
    ).json()["id"]

    tested = authed.post(
        f"/api/sites/{site_id}/feeds/test", json={"url": f"{blog.base}feed.xml"}, headers=XHR
    )
    assert tested.status_code == 200, tested.text
    assert tested.json()["ok"]
    assert tested.json()["entry_count"] == 2
    assert tested.json()["in_scope"] == 2, "the entries are on the site's own host"

    added = authed.post(
        f"/api/sites/{site_id}/feeds", json={"url": f"{blog.base}feed.xml"}, headers=XHR
    )
    assert added.status_code == 201, added.text
    feed_id = added.json()["id"]

    polled = authed.post(f"/api/feeds/{feed_id}/poll", headers=XHR)
    assert polled.status_code == 200, polled.text
    assert polled.json()["baseline"]
    assert polled.json()["entries_seen"] == 2
    assert polled.json()["job_ids"] == [], "a baseline captures nothing"

    history = authed.get(f"/api/feeds/{feed_id}/polls", headers=XHR).json()
    assert len(history) == 1
    assert history[0]["status"] == 200


def test_a_feed_whose_entries_are_out_of_scope_says_so(authed: TestClient, blog: Blog) -> None:
    """A real and confusing failure: it polls happily forever, finds new posts
    every time, and archives none of them."""
    site_id = authed.post(
        "/api/sites",
        json={"seed_url": "https://elsewhere.example/", "title": "Elsewhere"},
        headers=XHR,
    ).json()["id"]

    tested = authed.post(
        f"/api/sites/{site_id}/feeds/test", json={"url": f"{blog.base}feed.xml"}, headers=XHR
    ).json()

    assert tested["ok"], "the feed itself parses fine"
    assert tested["entry_count"] == 2
    assert tested["in_scope"] == 0
    assert len(tested["out_of_scope"]) == 2


def test_the_conditional_get_is_used_on_the_second_poll(authed: TestClient, blog: Blog) -> None:
    """What makes a short interval affordable."""
    site_id = authed.post(
        "/api/sites", json={"seed_url": blog.base, "title": "Blog"}, headers=XHR
    ).json()["id"]
    feed_id = authed.post(
        f"/api/sites/{site_id}/feeds", json={"url": f"{blog.base}feed.xml"}, headers=XHR
    ).json()["id"]

    authed.post(f"/api/feeds/{feed_id}/poll", headers=XHR)
    second = authed.post(f"/api/feeds/{feed_id}/poll", headers=XHR).json()

    assert second["status"] == 304
    assert second["action"] == "not modified"
    assert blog.conditional_requests == 1


def test_discovery_offers_feeds_and_sitemaps_without_adding_them(
    authed: TestClient, blog: Blog
) -> None:
    site_id = authed.post(
        "/api/sites", json={"seed_url": blog.base, "title": "Blog"}, headers=XHR
    ).json()["id"]

    found = authed.post(f"/api/sites/{site_id}/feeds/discover", headers=XHR).json()

    assert any(c["url"].endswith("/sitemap.xml") and c["kind"] == "sitemap" for c in found)
    assert authed.get(f"/api/sites/{site_id}/feeds", headers=XHR).json() == [], (
        "discovering must not attach anything"
    )


def test_a_comment_feed_arrives_switched_off(db: Session, site: Site) -> None:
    """Mostly noise, and after M6 that noise is real requests and real
    captures. The posts feed still arrives on."""
    from cairn.discovery.runner import DiscoveryResult
    from cairn.services import discovery_service

    result = DiscoveryResult(seed_url="https://blog.example/", seed_host="blog.example")
    result.feeds = [
        "https://blog.example/feeds/posts/default",
        "https://blog.example/feeds/comments/default",
    ]

    discovery_service.record_feeds(db, site, result)

    rows = db.scalars(select(Feed).where(Feed.site_id == site.id)).all()
    by_url = {feed.url: feed for feed in rows}
    assert by_url["https://blog.example/feeds/posts/default"].enabled
    comments = by_url["https://blog.example/feeds/comments/default"]
    assert not comments.enabled
    assert comments.disabled_reason


def test_re_enabling_a_failed_feed_clears_its_backoff(
    authed: TestClient, db: Session, site: Site
) -> None:
    feed = _feed_for(db, site)
    for _ in range(feeds.FAILURES_BEFORE_DISABLE):
        feeds.apply(db, feed, feeds.FetchResult(status=500, error="HTTP 500"))
    db.commit()
    assert not feed.enabled

    res = authed.patch(f"/api/feeds/{feed.id}", json={"enabled": True}, headers=XHR)

    assert res.status_code == 200, res.text
    assert res.json()["consecutive_failures"] == 0
    assert res.json()["disabled_reason"] is None
