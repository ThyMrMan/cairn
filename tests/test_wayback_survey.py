"""Wayback pre-fill, stage 1: the polite client, the CDX pull, the survey (docs/19).

Nothing here talks to archive.org. The client runs against `httpx.MockTransport`
with a fake clock, so a test of an hour's backoff takes milliseconds and proves
the wait happened rather than that a sleep was called.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Job, Site
from cairn.db.types import utcnow
from cairn.net.polite import (
    Pacing,
    PoliteClient,
    PoliteError,
    RateLimitedError,
    StopRequestedError,
    retry_after_seconds,
)
from cairn.services import storage
from cairn.services import wayback as wayback_service
from cairn.services.jobs import _busy_hosts, _host_for_job
from cairn.services.scope import HostRule, Scope
from cairn.wayback import cdx
from cairn.wayback import survey as wb
from tests.conftest import XHR


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept += seconds


def client_for(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    pacing: Pacing | None = None,
    clock: FakeClock | None = None,
    **kwargs: object,
) -> tuple[PoliteClient, FakeClock]:
    clock = clock or FakeClock()
    client = PoliteClient(
        pacing or Pacing(min_gap_s=5, jitter_s=0, backoff_start_s=60, max_attempts=8),
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        clock=clock,
        **kwargs,  # type: ignore[arg-type]
    )
    return client, clock


# ── the polite client ────────────────────────────────────────────────────


def test_retry_after_is_honoured() -> None:
    calls: list[float] = []
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(clock.now)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "300"})
        return httpx.Response(200, content=b"data")

    client, _ = client_for(handler, clock=clock)
    response = client.get("https://web.archive.org/x")
    assert response.body == b"data"
    assert calls[1] - calls[0] >= 300


def test_without_retry_after_the_wait_doubles() -> None:
    calls: list[float] = []
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(clock.now)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, content=b"ok")

    client, _ = client_for(handler, clock=clock)
    client.get("https://web.archive.org/x")
    assert calls[1] - calls[0] >= 60
    assert calls[2] - calls[1] >= 120


def test_a_run_of_429s_stops_the_client() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(429)

    client, _ = client_for(handler, pacing=Pacing(min_gap_s=1, jitter_s=0, max_consecutive_429=3))
    with pytest.raises(RateLimitedError):
        client.get("https://web.archive.org/x")
    assert requests == 3


def test_a_disguised_429_counts_as_one() -> None:
    """fictionmania redirects to /err429.html and serves it with 200."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/err429.html":
            return httpx.Response(200, content=b"<html>slow down</html>")
        return httpx.Response(302, headers={"Location": "/err429.html"})

    client, _ = client_for(
        handler,
        pacing=Pacing(min_gap_s=1, jitter_s=0, max_consecutive_429=2),
        soft_429=r"/err429\.html",
    )
    with pytest.raises(RateLimitedError):
        client.get("https://www.fictionmania.tv/stories/readtextstory.html?storyID=1")


def test_an_html_outage_page_is_not_data() -> None:
    bodies = [b"<!DOCTYPE html><title>Internet Archive: Temporarily Offline</title>", b"1 2 3\n"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=bodies.pop(0))

    client, _ = client_for(handler)
    assert client.get("https://web.archive.org/cdx", reject_html=True).body == b"1 2 3\n"


def test_giving_up_says_why() -> None:
    client, _ = client_for(
        lambda r: httpx.Response(502), pacing=Pacing(min_gap_s=0, jitter_s=0, max_attempts=2)
    )
    with pytest.raises(PoliteError, match="HTTP 502"):
        client.get("https://web.archive.org/x")


def test_a_404_is_an_answer_not_a_retry() -> None:
    client, _ = client_for(lambda r: httpx.Response(404))
    assert client.get("https://web.archive.org/x").status == 404
    assert client.requests == 1


def test_raw_mode_keeps_the_bytes_as_sent() -> None:
    """`id_` snapshots arrive gzipped and their digest is over those bytes."""
    payload = gzip.compress(b"<html>story</html>")

    def handler(request: httpx.Request) -> httpx.Response:
        # Streamed, as a real connection is: a pre-read body has no raw form.
        return httpx.Response(
            200, stream=httpx.ByteStream(payload), headers={"Content-Encoding": "gzip"}
        )

    client, _ = client_for(handler)
    raw = client.get("https://web.archive.org/web/2019id_/https://a.test/", raw=True)
    assert raw.body[:2] == b"\x1f\x8b"
    assert raw.body == payload
    decoded = client.get("https://web.archive.org/web/2019id_/https://a.test/")
    assert decoded.body == b"<html>story</html>"


def test_requests_to_one_host_keep_their_gap() -> None:
    times: list[float] = []
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        times.append(clock.now)
        return httpx.Response(200)

    client, _ = client_for(handler, clock=clock, pacing=Pacing(min_gap_s=10, jitter_s=0))
    for _ in range(3):
        client.get("https://web.archive.org/x")
    assert times[1] - times[0] >= 10 and times[2] - times[1] >= 10


def test_two_clients_share_a_host_budget() -> None:
    times: list[float] = []
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        times.append(clock.now)
        return httpx.Response(200)

    pacing = Pacing(min_gap_s=10, jitter_s=0)
    first, _ = client_for(handler, clock=clock, pacing=pacing)
    second, _ = client_for(handler, clock=clock, pacing=pacing)
    first.get("https://shared.test/a")
    second.get("https://shared.test/b")
    assert times[1] - times[0] >= 10


def test_a_stop_request_ends_a_long_backoff() -> None:
    clock = FakeClock()
    stop = {"now": False}

    def handler(request: httpx.Request) -> httpx.Response:
        stop["now"] = True
        return httpx.Response(503, headers={"Retry-After": "3600"})

    client, _ = client_for(handler, clock=clock, should_stop=lambda: stop["now"])
    with pytest.raises(StopRequestedError):
        client.get("https://web.archive.org/x")
    assert clock.slept < 2


def test_retry_after_can_be_a_date() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
    assert retry_after_seconds("Sun, 01 Nov 2026 12:02:00 GMT", now=now) == 120
    assert retry_after_seconds("45") == 45
    assert retry_after_seconds("soon") is None


# ── the CDX pull ─────────────────────────────────────────────────────────


def cdx_line(
    url: str, ts: str, status: str = "200", digest: str = "D", mime: str = "text/html"
) -> str:
    import surt

    return f"{surt.surt(url)} {ts} {url} {mime} {status} {digest} 1234"


def cdx_server(
    pages: list[list[str]],
) -> tuple[Callable[[httpx.Request], httpx.Response], list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if params.get("showNumPages"):
            seen.append("count")
            return httpx.Response(200, content=f"{len(pages)}\n".encode())
        page = int(params["page"])
        seen.append(f"page {page}")
        assert params["fl"] == ",".join(cdx.FIELDS)
        return httpx.Response(200, content=("\n".join(pages[page]) + "\n").encode())

    return handler, seen


def test_pull_saves_every_page_and_resumes(tmp_path: Path) -> None:
    pages = [
        [cdx_line("https://a.test/1", "20190101000000")],
        [cdx_line("https://a.test/2", "20200101000000")],
    ]
    handler, seen = cdx_server(pages)
    client, _ = client_for(handler)
    assert cdx.pull(client, "a.test/", tmp_path) == 2
    assert seen == ["count", "page 0", "page 1"]
    assert cdx.complete(tmp_path)
    assert [r.original for r in cdx.iter_rows(tmp_path)] == ["https://a.test/1", "https://a.test/2"]

    # A second run asks for nothing it already has.
    seen.clear()
    assert cdx.pull(client, "a.test/", tmp_path) == 2
    assert seen == []

    # A half-finished run continues from the first missing page.
    cdx.page_path(tmp_path, 1).unlink()
    cdx.pull(client, "a.test/", tmp_path)
    assert seen == ["page 1"]


def test_refresh_and_a_new_prefix_start_over(tmp_path: Path) -> None:
    handler, seen = cdx_server([[cdx_line("https://a.test/1", "20190101000000")]])
    client, _ = client_for(handler)
    cdx.pull(client, "a.test/", tmp_path)
    seen.clear()
    cdx.pull(client, "a.test/", tmp_path, refresh=True)
    assert seen == ["count", "page 0"]
    seen.clear()
    cdx.pull(client, "b.test/", tmp_path)
    assert seen == ["count", "page 0"]


def test_a_malformed_row_is_skipped() -> None:
    assert cdx.CdxRow.parse("too few fields") is None
    assert cdx.CdxRow.parse("k notadate u m 200 d 1") is None
    row = cdx.CdxRow.parse("k 20190101000000 https://a.test/ text/html;charset=utf-8 200 D -")
    assert row is not None and row.mimetype == "text/html" and row.length is None


# ── the reduction ────────────────────────────────────────────────────────

STORY = "https://www.fictionmania.tv/stories/{view}.html?storyID={id}"
STORIES = {
    "name": "stories",
    "pattern": r"/stories/(?P<view>read\w+story|details)\.html\?(?:.*&)?storyID=(?P<item>\d+)",
    "prefer": ["readtextstory", "readxstory", "readhtmlstory"],
    "keep": ["details"],
}


def fm_scope() -> Scope:
    return Scope(
        seeds=["https://www.fictionmania.tv/"],
        hosts=[
            HostRule("www.fictionmania.tv", crawl_pages=True),
            HostRule("cdn.test", fetch_assets=True),
        ],
        reject_patterns=["/review/"],
    )


def story(view: str, item: int) -> str:
    return STORY.format(view=view, id=item)


def rows(lines: list[str]) -> list[cdx.CdxRow]:
    parsed = [cdx.CdxRow.parse(line) for line in lines]
    return [r for r in parsed if r is not None]


def fm_rows() -> list[cdx.CdxRow]:
    lines = [
        # Story 1: two text views and a details page.
        cdx_line(story("readtextstory", 1), "20190101000000", digest="S1T"),
        cdx_line(story("readxstory", 1), "20200101000000", digest="S1X"),
        cdx_line(story("details", 1), "20190101000000", digest="S1D"),
        # Story 2: only the second-choice view.
        cdx_line(story("readxstory", 2), "20210101000000", digest="S2X"),
        # Story 3: the newest snapshot is the rate-limit page.
        cdx_line(story("readtextstory", 3), "20240101000000", digest="ERR"),
        cdx_line(story("readtextstory", 3), "20170101000000", digest="S3T"),
        # Story 4: only ever the rate-limit page.
        cdx_line(story("readtextstory", 4), "20250101000000", digest="ERR"),
        # The rate-limit page itself.
        cdx_line("https://www.fictionmania.tv/err429.html", "20240101000000", digest="ERR"),
        # Out of scope: rejected pattern, foreign host.
        cdx_line("https://www.fictionmania.tv/review/readreview.html?id=1", "20190101000000"),
        cdx_line("https://elsewhere.test/page", "20190101000000"),
        # Not a capture: a revisit and a redirect.
        cdx_line(story("readtextstory", 5), "20220101000000", status="-"),
        cdx_line(story("readtextstory", 6), "20220101000000", status="302"),
        # An asset, and a page on the asset-only host.
        cdx_line("https://cdn.test/a.jpg", "20180101000000", mime="image/jpeg", digest="IMG"),
        cdx_line("https://cdn.test/page", "20180101000000"),
    ]
    # Twenty-five different pages with one identical body: boilerplate.
    lines += [
        cdx_line(f"https://www.fictionmania.tv/removed/{n}", "20230101000000", digest="BOIL")
        for n in range(25)
    ]
    return rows(lines)


def test_the_survey_counts_what_an_import_would_fetch() -> None:
    result = wb.reduce(
        fm_rows(),
        wb.ScopeFilter(fm_scope()),
        wb.parse_groups([STORIES]),
        rate_limit_page=r"/err429\.html",
    )
    picked = {(s.item, s.view): s for s in result.selected if s.group == "stories"}
    # Story 1: the preferred view plus details; the other view is skipped.
    assert ("1", "readtextstory") in picked and ("1", "details") in picked
    assert ("1", "readxstory") not in picked
    # Story 2: falls through to the next preference.
    assert ("2", "readxstory") in picked
    # Story 3: the error-page snapshot is passed over for the older real one.
    assert picked[("3", "readtextstory")].timestamp == "20170101000000"
    # Story 4: nothing usable.
    assert not any(item == "4" for item, _ in picked)

    report = result.report
    stories = report["groups"][0]
    assert stories["items"] == 3
    assert stories["fetches"] == 4
    assert stories["skipped_variants"] == 1
    assert report["out_of_scope"]["reject: /review/"] == 1
    assert report["out_of_scope"]["host not in scope"] == 1
    assert report["out_of_scope"]["page on an assets-only host"] == 1
    assert report["boilerplate"]["rate_limit_digests"] == ["ERR"]
    assert report["boilerplate"]["shared_digests"] == {"BOIL": 25}
    assert report["urls_only_error_pages"] == 26  # story 4 and the 25 boilerplate pages
    assert report["selected"] == {"total": 5, "pages": 4, "assets": 1}
    assert report["freshness"]["pages"] == {"2017": 1, "2019": 2, "2021": 1}
    # The breakdown a user reads to decide what else to reject.
    sections = {(s["section"], s["kind"]): s["count"] for s in report["sections"]}
    assert sections[("group: stories", "readtextstory")] == 2
    assert sections[("/a.jpg", "asset")] == 1
    # Pages come first, so a budget that runs out leaves text, not pictures.
    assert [s.kind for s in result.selected] == ["page"] * 4 + ["asset"]


def test_without_groups_every_url_is_its_own_item() -> None:
    result = wb.reduce(fm_rows(), wb.ScopeFilter(fm_scope()), [], rate_limit_page=r"/err429\.html")
    assert result.report["groups"] == []
    assert result.report["selected"]["pages"] == 5


def test_the_newest_good_snapshot_wins_and_older_ones_are_fallbacks() -> None:
    lines = [
        cdx_line("https://www.fictionmania.tv/p", ts, digest=f"D{ts[:4]}")
        for ts in (
            "20150101000000",
            "20230101000000",
            "20190101000000",
            "20210101000000",
            "20170101000000",
        )
    ]
    result = wb.reduce(rows(lines), wb.ScopeFilter(fm_scope()), [])
    (only,) = result.selected
    assert only.timestamp == "20230101000000"
    assert [ts for ts, _ in only.older] == ["20210101000000", "20190101000000", "20170101000000"]


def test_discovered_urls_are_compared_by_item() -> None:
    discovered = [
        story("readhtmlstory", 2),  # another view of a story Wayback covers
        story("readtextstory", 99),  # a story Wayback never saw
        "https://www.fictionmania.tv/removed/1",  # boilerplate only: missing
    ]
    result = wb.reduce(
        fm_rows(),
        wb.ScopeFilter(fm_scope()),
        wb.parse_groups([STORIES]),
        rate_limit_page=r"/err429\.html",
        discovered=discovered,
    )
    assert result.report["discovered"]["in_wayback"] == 1
    assert result.report["discovered"]["missing_from_wayback"] == 2


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"name": "", "pattern": "(?P<item>x)"}, "needs a name"),
        ({"name": "a", "pattern": "("}, "invalid pattern"),
        ({"name": "a", "pattern": "x"}, "item"),
        ({"name": "a", "pattern": "(?P<item>x)", "prefer": ["v"]}, "view"),
    ],
)
def test_a_bad_group_is_refused_with_a_reason(entry: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        wb.parse_groups([entry])


def test_a_whole_survey_writes_its_report_and_selection(tmp_path: Path) -> None:
    lines = [
        cdx_line(story("readtextstory", 1), "20190101000000", digest="A"),
        cdx_line(story("details", 1), "20190101000000", digest="B"),
    ]
    handler, _ = cdx_server([lines])
    client, _ = client_for(handler)
    inputs = wb.Inputs(
        directory=tmp_path,
        scope=fm_scope(),
        groups=wb.parse_groups([STORIES]),
        rate_limit_page=None,
        discovered=None,
        rate_per_min=10,
    )
    report = wb.run(inputs, client)
    assert report["hosts"] == ["fictionmania.tv"]
    assert report["selected"]["total"] == 2
    assert wb.read_report(tmp_path) == json.loads((tmp_path / wb.SURVEY_FILE).read_text())
    selection = gzip.decompress((tmp_path / wb.SELECTION_FILE).read_bytes()).decode().splitlines()
    assert {json.loads(line)["view"] for line in selection} == {"readtextstory", "details"}


def test_a_scope_with_no_crawlable_host_is_refused(tmp_path: Path) -> None:
    scope = Scope(seeds=["https://a.test/"], hosts=[HostRule("cdn.test", fetch_assets=True)])
    inputs = wb.Inputs(tmp_path, scope, [], None, None, 10)
    client, _ = client_for(lambda r: httpx.Response(200))
    with pytest.raises(wb.SurveyError):
        wb.run(inputs, client)


# ── the job queue: a survey loads archive.org, not the site ──────────────


def make_site(db: Session, settings: Settings, slug: str, host: str) -> Site:
    site = Site(
        folder_id=1,
        slug=slug,
        title=slug,
        seed_url=f"https://{host}/",
        primary_host=host,
        archive_path=f"Unfiled/{slug}",
    )
    db.add(site)
    db.flush()
    storage.ensure_site_dirs(settings, site.archive_path)
    return site


def make_job(db: Session, site: Site, kind: str, status: str) -> Job:
    job = Job(type=kind, site_id=site.id, status=status, spec={}, queued_at=utcnow())
    db.add(job)
    db.flush()
    return job


def test_a_survey_queues_on_archive_org(
    authed: TestClient, db: Session, settings: Settings
) -> None:
    fm = make_site(db, settings, "fm", "www.fictionmania.tv")
    other = make_site(db, settings, "other", "other.test")
    survey = make_job(db, fm, wayback_service.JOB_TYPE, "running")
    assert _host_for_job(db, survey) == "web.archive.org"
    assert _busy_hosts(db) == {"web.archive.org"}

    # Another site's survey waits; a crawl of the surveyed site does not.
    assert _host_for_job(
        db, make_job(db, other, wayback_service.JOB_TYPE, "queued")
    ) in _busy_hosts(db)
    assert _host_for_job(db, make_job(db, fm, "capture", "queued")) not in _busy_hosts(db)

    make_job(db, fm, "capture", "running")
    assert _busy_hosts(db) == {"web.archive.org", "www.fictionmania.tv"}


# ── the API ──────────────────────────────────────────────────────────────


def test_options_round_trip_and_bad_ones_are_refused(
    authed: TestClient, db: Session, settings: Settings
) -> None:
    site = make_site(db, settings, "fm", "www.fictionmania.tv")
    db.commit()
    body = {"url_groups": [STORIES], "rate_limit_page": r"/err429\.html", "live_items": 60000}
    saved = authed.put(f"/api/sites/{site.id}/wayback/options", json=body, headers=XHR)
    assert saved.status_code == 200, saved.text
    assert saved.json()["options"]["url_groups"][0]["prefer"][0] == "readtextstory"

    state = authed.get(f"/api/sites/{site.id}/wayback", headers=XHR).json()
    assert state["options"]["live_items"] == 60000
    assert state["survey"] is None and state["job"] is None

    bad = authed.put(
        f"/api/sites/{site.id}/wayback/options",
        json={"url_groups": [{"name": "x", "pattern": "no-item-group"}]},
        headers=XHR,
    )
    assert bad.status_code == 422
    assert "item" in bad.json()["error"]["message"]

    cleared = authed.put(f"/api/sites/{site.id}/wayback/options", json={}, headers=XHR)
    assert cleared.json()["options"] == {
        "url_groups": [],
        "rate_limit_page": None,
        "live_items": None,
    }


def test_one_survey_at_a_time(
    authed: TestClient, db: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The test app runs the real job queue, so the survey really starts. Its
    archive.org is a fake that holds the first request until the test ends."""
    import threading

    release = threading.Event()
    asked = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        asked.set()
        release.wait(30)
        return httpx.Response(200, content=b"0" + bytes([10]))

    def fake_client(agent: str, **hooks: object) -> PoliteClient:
        return PoliteClient(
            Pacing(min_gap_s=0, jitter_s=0),
            transport=httpx.MockTransport(handler),
            **hooks,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(wayback_service, "cdx_client", fake_client)
    site = make_site(db, settings, "fm", "www.fictionmania.tv")
    db.commit()
    try:
        started = authed.post(f"/api/sites/{site.id}/wayback/survey", json={}, headers=XHR)
        assert started.status_code == 202
        again = authed.post(
            f"/api/sites/{site.id}/wayback/survey", json={"refresh": True}, headers=XHR
        )
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "survey_busy"
        state = authed.get(f"/api/sites/{site.id}/wayback", headers=XHR).json()
        assert state["job"]["status"] in ("queued", "running")
        assert asked.wait(10), "the survey never asked archive.org"
    finally:
        release.set()


def test_a_saved_report_is_served(authed: TestClient, db: Session, settings: Settings) -> None:
    site = make_site(db, settings, "fm", "www.fictionmania.tv")
    db.commit()
    wb.write(wayback_service.directory(settings, site), {"version": 1, "rows": 3}, [])
    state = authed.get(f"/api/sites/{site.id}/wayback", headers=XHR).json()
    assert state["survey"] == {"version": 1, "rows": 3}


def test_the_plainest_address_of_an_item_wins_over_a_tracking_variant() -> None:
    """Found on the real listing: Facebook-shared `&mibextid=` copies were newer."""
    plain = story("readtextstory", 7)
    tracked = plain + "&mibextid=AFdixm"
    lines = [
        cdx_line(plain, "20190101000000", digest="PLAIN"),
        cdx_line(tracked, "20231129040243", digest="TRACKED"),
    ]
    result = wb.reduce(rows(lines), wb.ScopeFilter(fm_scope()), wb.parse_groups([STORIES]))
    (only,) = result.selected
    assert only.url == plain
