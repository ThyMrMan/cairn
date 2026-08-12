"""The reader view, site health, and a pasted list of URLs.

Three unrelated features that share a shape: each is a small amount of code
around one decision that would be wrong if it were made the obvious way.

  - The reader must not silently stand in for a broken replay.
  - "The site is gone" must not be said on one bad response.
  - A pasted URL is a page, not a site, and importing fifty of them must not
    crawl fifty sites.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, PageText, Site, SiteHealth
from cairn.db.types import utcnow
from cairn.discovery.fetch import Fetcher
from cairn.services import bulkurls, reader, sitehealth, textextract
from cairn.services import sites as site_service
from tests.conftest import XHR

# ── the reader view ──────────────────────────────────────────────────────

ARTICLE = """<!doctype html><html><head><title>A post about filters</title></head>
<body>
<nav class="sidebar"><a href="/a">Every post title, on every page</a></nav>
<article class="post-body">
<h2>The heading of the section</h2>
<p>The first paragraph, which is long enough to survive the minimum block length.</p>
<ul><li>An item in a list, also long enough to keep.</li></ul>
<blockquote>Somebody else said this, at some length, in a quotation.</blockquote>
</article>
</body></html>"""


def test_extraction_records_what_each_block_was() -> None:
    """The reader needs it; search does not, and never looks at it."""
    title, blocks, kinds = textextract.parse_kinds(ARTICLE)
    assert title == "A post about filters"
    assert len(blocks) == len(kinds)
    by_kind = dict(zip(kinds, blocks, strict=True))
    assert by_kind["h2"].startswith("The heading")
    assert by_kind["li"].startswith("An item")
    assert by_kind["quote"].startswith("Somebody else")
    assert any(k == "p" for k in kinds)


def test_a_file_written_before_kinds_existed_still_reads() -> None:
    """Derived data is regenerable, but not on the day somebody upgrades."""
    page = textextract.Page(url="u", title="t", blocks=["one", "two"], kinds=[])
    assert page.kind_of(0) == "p"
    assert page.kind_of(1) == "p"
    assert page.kind_of(99) == "p"


def test_dropping_repeated_blocks_keeps_the_kinds_lined_up() -> None:
    """Two positional lists, filtered separately, is a silent corruption.

    Every heading after the dropped block would take the kind of the one
    before it, which reads as a page whose structure is subtly wrong and whose
    text is perfectly right.
    """
    pages = [
        textextract.Page(
            url=f"u{i}",
            title="t",
            blocks=["a sidebar block repeated everywhere", f"heading {i}", f"body {i}"],
            kinds=["p", "h2", "p"],
        )
        for i in range(4)
    ]
    textextract._drop_repeated(pages)
    for page in pages:
        assert len(page.blocks) == len(page.kinds)
        assert page.blocks[0].startswith("heading")
        assert page.kind_of(0) == "h2"


def _readable_site(db: Session, settings: Settings) -> Site:
    site = site_service.create_site(db, settings, seed_url="https://read.example.com/")
    capture_dir = "20260101-000000-full"
    path = textextract.text_path(settings, site.archive_path, capture_dir)
    page = textextract.Page(
        url="https://read.example.com/post.html",
        title="A post about filters",
        blocks=["The heading", "The body of the post, at some length."],
        kinds=["h2", "p"],
        timestamp="20260101000000",
    )
    textextract._write_jsonl(path, [page])
    db.add(
        Capture(
            site_id=site.id,
            kind="full",
            engine_id="wget-warc",
            dir_name=capture_dir,
            status="ok",
        )
    )
    db.add(
        PageText(
            site_id=site.id,
            capture_dir=capture_dir,
            url=page.url,
            title=page.title,
            timestamp=page.timestamp,
            offset=page.offset,
            length=page.length,
            words=8,
        )
    )
    db.flush()
    return site


def test_a_page_reads_as_structured_text(db: Session, settings: Settings) -> None:
    site = _readable_site(db, settings)
    article = reader.read(db, settings, site, "https://read.example.com/post.html")
    assert article is not None
    assert article.title == "A post about filters"
    assert [b.kind for b in article.blocks] == ["h2", "p"]
    assert article.words == 10
    assert article.capture_dir == "20260101-000000-full"


def test_a_stale_offset_falls_back_to_a_scan(db: Session, settings: Settings) -> None:
    """The index points into a file that can be rewritten underneath it.

    Serving whatever landed at that byte would be worse than a slow answer, so
    a mismatched URL means scan the file instead.
    """
    site = _readable_site(db, settings)
    row = db.scalars(select(PageText)).first()
    assert row is not None
    row.offset = 9_999
    row.length = 40
    db.flush()

    article = reader.read(db, settings, site, "https://read.example.com/post.html")
    assert article is not None
    assert article.title == "A post about filters"


def test_a_page_with_no_extracted_text_says_so(authed: TestClient, settings: Settings) -> None:
    created = authed.post(
        "/api/sites", json={"seed_url": "https://empty.example.com/"}, headers=XHR
    )
    site_id = created.json()["id"]
    response = authed.get(
        f"/api/sites/{site_id}/reader?url=https://empty.example.com/x", headers=XHR
    )
    assert response.status_code == 404
    assert "Rebuild search index" in response.json()["error"]["message"]


def test_the_reader_lists_what_it_can_show(db: Session, settings: Settings) -> None:
    site = _readable_site(db, settings)
    found = reader.versions(db, settings, site, "https://read.example.com/post.html")
    assert [v.capture_dir for v in found] == ["20260101-000000-full"]
    assert reader.versions(db, settings, site, "https://read.example.com/missing") == []


# ── site health ──────────────────────────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    code = 200
    location = ""

    def do_GET(self) -> None:
        self._respond()

    def do_HEAD(self) -> None:
        self._respond(body=False)

    def _respond(self, body: bool = True) -> None:
        payload = b"<html><body>hello</body></html>"
        self.send_response(type(self).code)
        if type(self).location:
            self.send_header("Location", type(self).location)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if body:
            self.wfile.write(payload)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def live_server() -> Iterator[type[_Handler]]:
    handler = type("_Bound", (_Handler,), {"code": 200, "location": ""})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    handler.port = server.server_address[1]  # type: ignore[attr-defined]
    try:
        yield handler
    finally:
        server.shutdown()
        server.server_close()


def _url(handler: type[_Handler]) -> str:
    return f"http://127.0.0.1:{handler.port}/"  # type: ignore[attr-defined]


async def test_a_living_site_reads_as_live(live_server: type[_Handler]) -> None:
    async with Fetcher() as fetcher:
        found = await sitehealth.probe(fetcher, _url(live_server))
    assert found.state == sitehealth.LIVE


async def test_a_404_reads_as_gone(live_server: type[_Handler]) -> None:
    live_server.code = 404
    async with Fetcher() as fetcher:
        found = await sitehealth.probe(fetcher, _url(live_server))
    assert found.state == sitehealth.GONE
    assert found.http_status == 404


async def test_a_server_error_is_not_gone(live_server: type[_Handler]) -> None:
    """A 500 is the site failing, not the site ending."""
    live_server.code = 503
    async with Fetcher() as fetcher:
        found = await sitehealth.probe(fetcher, _url(live_server))
    assert found.state == sitehealth.UNREACHABLE


async def test_a_403_is_about_us(live_server: type[_Handler]) -> None:
    live_server.code = 403
    async with Fetcher() as fetcher:
        found = await sitehealth.probe(fetcher, _url(live_server))
    assert found.state == sitehealth.BLOCKED


async def test_nothing_listening_is_unreachable() -> None:
    async with Fetcher() as fetcher:
        found = await sitehealth.probe(fetcher, "http://127.0.0.1:9/")
    assert found.state == sitehealth.UNREACHABLE
    assert found.error


def test_one_bad_check_does_not_kill_a_site(db: Session, settings: Settings) -> None:
    """The whole point of the confirmation counter.

    A blog is briefly 502 and a container is briefly without DNS; announcing
    that somebody's site is gone on one response would make the notification
    worthless within a month.
    """
    site = site_service.create_site(db, settings, seed_url="https://flaky.example.com/")
    assert sitehealth.record(db, site, sitehealth.Probe(state=sitehealth.LIVE)) is None

    first = sitehealth.record(db, site, sitehealth.Probe(state=sitehealth.GONE, http_status=404))
    assert first is None, "one bad check must not change the state"
    assert db.get(SiteHealth, site.id).state == sitehealth.LIVE

    second = sitehealth.record(db, site, sitehealth.Probe(state=sitehealth.GONE, http_status=404))
    assert second == sitehealth.GONE
    assert db.get(SiteHealth, site.id).state == sitehealth.GONE


def test_a_recovery_clears_the_pending_state(db: Session, settings: Settings) -> None:
    site = site_service.create_site(db, settings, seed_url="https://blip.example.com/")
    sitehealth.record(db, site, sitehealth.Probe(state=sitehealth.LIVE))
    sitehealth.record(db, site, sitehealth.Probe(state=sitehealth.GONE, http_status=404))
    sitehealth.record(db, site, sitehealth.Probe(state=sitehealth.LIVE))

    row = db.get(SiteHealth, site.id)
    assert row is not None
    assert row.state == sitehealth.LIVE
    assert row.pending_state is None
    assert row.consecutive == 0


def test_the_first_check_of_a_dead_site_announces_nothing(db: Session, settings: Settings) -> None:
    """It was archived precisely because it was disappearing."""
    site = site_service.create_site(db, settings, seed_url="https://already.example.com/")
    changed = sitehealth.record(db, site, sitehealth.Probe(state=sitehealth.GONE, http_status=404))
    assert changed is None
    assert db.get(SiteHealth, site.id).state == sitehealth.GONE


def test_never_checked_sites_come_first(db: Session, settings: Settings) -> None:
    now = utcnow()
    checked = site_service.create_site(db, settings, seed_url="https://checked.example.com/")
    fresh = site_service.create_site(db, settings, seed_url="https://fresh.example.com/")
    db.add(
        SiteHealth(
            site_id=checked.id,
            state=sitehealth.LIVE,
            checked_at=now - timedelta(days=30),
            since=now,
        )
    )
    db.flush()
    due = sitehealth.due_sites(db, now=now, days=7)
    assert due[0].id == fresh.id
    assert {s.id for s in due} == {fresh.id, checked.id}


def test_a_site_checked_yesterday_is_not_due(db: Session, settings: Settings) -> None:
    now = utcnow()
    site = site_service.create_site(db, settings, seed_url="https://recent.example.com/")
    db.add(
        SiteHealth(
            site_id=site.id, state=sitehealth.LIVE, checked_at=now - timedelta(days=1), since=now
        )
    )
    db.flush()
    assert sitehealth.due_sites(db, now=now, days=7) == []


def test_the_summary_leads_with_what_is_gone(db: Session, settings: Settings) -> None:
    now = utcnow()
    gone = site_service.create_site(db, settings, seed_url="https://gone.example.com/")
    blocked = site_service.create_site(db, settings, seed_url="https://blocked.example.com/")
    db.add(SiteHealth(site_id=blocked.id, state=sitehealth.BLOCKED, checked_at=now, since=now))
    db.add(SiteHealth(site_id=gone.id, state=sitehealth.GONE, checked_at=now, since=now))
    db.flush()

    summary = sitehealth.summary(db)
    assert [p["state"] for p in summary["problems"]] == [sitehealth.GONE, sitehealth.BLOCKED]
    assert summary["counts"][sitehealth.GONE] == 1


def test_health_reaches_the_digest_but_only_when_it_matters(
    db: Session, settings: Settings
) -> None:
    """`unreachable` says more about this end than theirs."""
    from cairn.services import digest as digest_service

    now = utcnow()
    gone = site_service.create_site(db, settings, seed_url="https://vanished.example.com/")
    flaky = site_service.create_site(db, settings, seed_url="https://offline.example.com/")
    db.add(SiteHealth(site_id=gone.id, state=sitehealth.GONE, checked_at=now, since=now))
    db.add(SiteHealth(site_id=flaky.id, state=sitehealth.UNREACHABLE, checked_at=now, since=now))
    db.flush()

    report = digest_service.build(db, settings, since=now - timedelta(days=7), now=now)
    assert [s["site_id"] for s in report.vanished_sites] == [gone.id]
    assert report.has_problems


# ── a pasted list of URLs ────────────────────────────────────────────────


def test_urls_are_pulled_out_of_whatever_was_pasted() -> None:
    """A bookmark export, a markdown list and a CSV column all work."""
    urls, skipped = bulkurls.extract(
        """
        <DT><A HREF="https://one.example.com/post" ADD_DATE="1">One</A>
        - [Two](https://two.example.com/post),
        "https://three.example.com/post",
        https://one.example.com/post#comments
        mailto:nobody@example.com
        """
    )
    assert urls == [
        "https://one.example.com/post",
        "https://two.example.com/post",
        "https://three.example.com/post",
    ]
    assert skipped == []


def test_a_site_is_seeded_at_the_origin_not_at_the_bookmark(
    db: Session, settings: Settings
) -> None:
    """Otherwise the site's identity is one post, forever."""
    plan = bulkurls.survey(db, "https://blog.example.com/2019/03/some-post.html")
    assert plan.groups[0].origin == "https://blog.example.com"

    result = bulkurls.import_urls(db, settings, "https://blog.example.com/2019/03/x.html")
    site = db.get(Site, result.created[0])
    assert site is not None
    assert site.seed_url == "https://blog.example.com/"


def test_pages_on_two_hosts_of_one_domain_are_one_site_and_both_in_scope(
    db: Session, settings: Settings
) -> None:
    """Grouping by registrable domain is what makes a group span hosts.

    A scope built from the first URL's host silently rejects everything on the
    other, which reads as a capture that archived half the list and said
    nothing about why.
    """
    text = "https://example.com/a\nhttps://www.example.com/b\n"
    plan = bulkurls.survey(db, text)
    assert len(plan.groups) == 1
    assert set(plan.groups[0].hosts) == {"example.com", "www.example.com"}

    result = bulkurls.import_urls(db, settings, text)
    site = db.get(Site, result.created[0])
    assert site is not None
    scope = site_service.resolved_scope(db, site)
    assert {rule.host for rule in scope.hosts} >= {"example.com", "www.example.com"}


def test_two_blogspot_subdomains_are_two_sites(db: Session, settings: Settings) -> None:
    """The PSL's private section, doing the job it does for the domain picker."""
    plan = bulkurls.survey(db, "https://alice.blogspot.com/a\nhttps://bob.blogspot.com/b\n")
    assert {g.key for g in plan.groups} == {"alice.blogspot.com", "bob.blogspot.com"}


def test_an_existing_site_is_reused_rather_than_duplicated(db: Session, settings: Settings) -> None:
    existing = site_service.create_site(db, settings, seed_url="https://known.example.com/")
    plan = bulkurls.survey(db, "https://www.known.example.com/a-post")
    assert plan.groups[0].site_id == existing.id

    result = bulkurls.import_urls(db, settings, "https://www.known.example.com/a-post")
    assert result.created == []
    assert result.updated == [existing.id]


class _FakeSupervisor:
    def __init__(self) -> None:
        self.specs: list[dict[str, object]] = []
        self._next = 1

    def enqueue(self, _session: Session, **kwargs: object) -> object:
        self.specs.append(dict(kwargs))
        job = type("Job", (), {"id": self._next})()
        self._next += 1
        return job

    def notify(self) -> None:
        pass


def test_importing_a_list_archives_the_pages_and_does_not_crawl(
    db: Session, settings: Settings
) -> None:
    """The safety property.

    Fifty bookmarks across fifty domains, each triggering a full crawl, is a
    plausible way to get an IP address blocked and a certain way to fill a
    disk. Crawling is available and never assumed.
    """
    supervisor = _FakeSupervisor()
    bulkurls.import_urls(
        db,
        settings,
        "https://a.example.com/1\nhttps://a.example.com/2\nhttps://b.example.org/1\n",
        supervisor=supervisor,
    )
    assert len(supervisor.specs) == 2
    for call in supervisor.specs:
        spec = call["spec"]
        assert spec["only_extra_seeds"] is True  # type: ignore[index]
        assert spec["extra_seeds"]  # type: ignore[index]


def test_asking_for_a_crawl_gets_one(db: Session, settings: Settings) -> None:
    supervisor = _FakeSupervisor()
    bulkurls.import_urls(db, settings, "https://c.example.com/1", supervisor=supervisor, crawl=True)
    spec = supervisor.specs[0]["spec"]
    assert spec["only_extra_seeds"] is False  # type: ignore[index]
    assert spec["kind"] == "full"  # type: ignore[index]


def test_the_import_round_trips_through_the_api(authed: TestClient) -> None:
    text = "https://api-import.example.com/one\nhttps://api-import.example.com/two"
    survey = authed.post("/api/import/urls/survey", json={"text": text}, headers=XHR)
    assert survey.status_code == 200, survey.text
    assert survey.json()["found"] == 2
    assert survey.json()["new_sites"] == 1

    done = authed.post("/api/import/urls", json={"text": text}, headers=XHR)
    assert done.status_code == 201, done.text
    body = done.json()
    assert len(body["created"]) == 1
    assert body["urls"] == 2

    listed = authed.get("/api/sites", headers=XHR).json()
    assert any(s["primary_host"] == "api-import.example.com" for s in listed["items"])


def test_a_paste_with_no_urls_is_not_an_error(authed: TestClient) -> None:
    response = authed.post(
        "/api/import/urls/survey", json={"text": "nothing here but words"}, headers=XHR
    )
    assert response.status_code == 200
    assert response.json() == {
        "found": 0,
        "groups": [],
        "new_sites": 0,
        "existing_sites": 0,
        "skipped": [],
        "skipped_count": 0,
    }


def test_an_enormous_paste_is_refused_with_a_reason() -> None:
    with pytest.raises(bulkurls.BulkImportError, match="4 MB"):
        bulkurls.extract("https://x.example.com/a " * 400_000)
