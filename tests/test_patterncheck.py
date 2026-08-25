"""Turning a URL-shape row into a skip pattern, and saying what it matches.

Both exist because of the same failure: a reject pattern is written blind, and
one that is valid but matches nothing is indistinguishable from one that works
until somebody counts a crawl an hour later.

The specific case was `/feeds/#/comments/default` typed into the pattern box.
That is the *report's* notation — `#` means "a numeric segment" — and as a
regex it is a literal `#`, which no fetched URL contains because fragments are
stripped before the request. It compiled, it saved, and 35% of the crawl went
on the URLs it was supposed to stop.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, CaptureUrl
from cairn.services import patterncheck, skiplist
from cairn.services import sites as site_service
from cairn.services.scope import HostRule, Scope
from cairn.services.urlshapes import pattern_for, summarize
from tests.conftest import XHR

BLOG = "https://b.blogspot.com"


# ── the notation is not a regex ──────────────────────────────────────────


def test_a_shape_pasted_as_a_regex_matches_nothing() -> None:
    """The bug, pinned. If this ever starts passing, the notation changed."""
    typed = re.compile("/feeds/#/comments/default")
    assert not typed.search(f"{BLOG}/feeds/8188981199832925103/comments/default")


def test_the_generated_pattern_matches_what_the_shape_stood_for() -> None:
    pattern = pattern_for("/feeds/#/comments/default", f"{BLOG}/feeds/1/comments/default")
    assert pattern is not None
    compiled = re.compile(pattern)
    assert compiled.search(f"{BLOG}/feeds/8188981199832925103/comments/default")
    assert compiled.search(f"{BLOG}/feeds/1234/comments/default?alt=json")
    # And not what it did not stand for.
    assert not compiled.search(f"{BLOG}/feeds/posts/default")
    assert not compiled.search(f"{BLOG}/feeds/1/comments/default/extra")
    assert not compiled.search(f"{BLOG}/2019/05/post.html")


@pytest.mark.parametrize(
    ("shape", "example", "hits", "misses"),
    [
        (
            "/#/#",
            f"{BLOG}/2019/05/",
            [f"{BLOG}/2019/05", f"{BLOG}/2019/05/"],
            [f"{BLOG}/2019/05/post.html", f"{BLOG}/blog/05"],
        ),
        (
            "/#/#/*.html",
            f"{BLOG}/2019/05/a-post.html",
            [f"{BLOG}/2019/05/a-post.html", f"{BLOG}/2019/05/other.html?m=1"],
            [f"{BLOG}/2019/05/a-post.jpg", f"{BLOG}/2019/05/"],
        ),
        (
            "/search/label/*",
            f"{BLOG}/search/label/Travel",
            [f"{BLOG}/search/label/Travel", f"{BLOG}/search/label/Food"],
            [f"{BLOG}/search/label/", f"{BLOG}/search"],
        ),
        (
            # Query keys are sorted in a shape and arbitrary in a URL, so a
            # literal `?a&b` would match only the ordering one server used.
            "/search?max-results&updated-max",
            f"{BLOG}/search?updated-max=2019-05-01&max-results=7",
            [
                f"{BLOG}/search?updated-max=2019-05-01&max-results=7",
                f"{BLOG}/search?max-results=7&updated-max=2019-05-01",
            ],
            [f"{BLOG}/search?max-results=7", f"{BLOG}/search"],
        ),
    ],
)
def test_shapes_convert_to_patterns_that_mean_the_same_thing(
    shape: str, example: str, hits: list[str], misses: list[str]
) -> None:
    pattern = pattern_for(shape, example)
    assert pattern is not None, shape
    compiled = re.compile(pattern)
    for url in hits:
        assert compiled.search(url), f"{shape} should match {url}"
    for url in misses:
        assert not compiled.search(url), f"{shape} should not match {url}"


def test_a_pattern_that_misses_its_own_example_is_not_offered() -> None:
    """The self-check that makes the button trustworthy.

    A generated pattern that does not match the URL it was generated from is
    the same silent no-op this whole feature exists to prevent, so it is
    withheld rather than offered.
    """
    assert pattern_for("/feeds/#/comments/default", f"{BLOG}/something/else") is None


def test_generated_patterns_avoid_escapes_javascript_would_reject() -> None:
    """Both engines compile the same reject set (docs/05), and browsertrix
    hands it to `new RegExp`. `re.escape` emits `\\-` and `\\&`, which are
    identity escapes JavaScript rejects under the `u` flag."""
    pattern = pattern_for("/a-b/c&d/*.html", f"{BLOG}/a-b/c&d/x.html")
    assert pattern is not None
    assert "\\-" not in pattern
    assert "\\&" not in pattern
    assert re.compile(pattern).search(f"{BLOG}/a-b/c&d/x.html")


def test_every_shape_of_a_real_capture_converts(db: Session, settings: Settings) -> None:
    """The property that matters across the whole report rather than one row:
    every shape it prints can be turned into a pattern that matches the
    example printed beside it."""
    site = site_service.create_site(db, settings, seed_url=f"{BLOG}/", folder_id=1)
    capture = _capture(db, site.id)
    _urls(
        db,
        capture.id,
        [f"{BLOG}/2019/{m:02d}/post-{n}.html" for m in range(1, 13) for n in range(20)]
        + [f"{BLOG}/feeds/{n}/comments/default" for n in range(300)]
        + [f"{BLOG}/search/label/label-{n}" for n in range(40)]
        + [f"{BLOG}/search?updated-max=2019-05-0{n}&max-results=7" for n in range(9)]
        + [f"{BLOG}/", f"{BLOG}/p/about.html"],
    )

    report = summarize(db, capture.id, limit=50)
    assert report["shapes"], "fixture produced no shapes"
    for row in report["shapes"]:
        assert row["pattern"] is not None, row["shape"]
        assert re.search(row["pattern"], row["example"]), row["shape"]


# ── what a pattern would match ───────────────────────────────────────────


def _capture(db: Session, site_id: int) -> Capture:
    capture = Capture(
        site_id=site_id, kind="full", engine_id="wget-warc", dir_name="c1", status="ok"
    )
    db.add(capture)
    db.flush()
    return capture


def _urls(db: Session, capture_id: int, urls: list[str]) -> None:
    for url in urls:
        db.add(CaptureUrl(capture_id=capture_id, url=url, host="b.blogspot.com", status_code=200))
    db.flush()


def test_a_pattern_that_matches_nothing_says_so(db: Session, settings: Settings) -> None:
    site = site_service.create_site(db, settings, seed_url=f"{BLOG}/", folder_id=1)
    capture = _capture(db, site.id)
    _urls(db, capture.id, [f"{BLOG}/feeds/{n}/comments/default" for n in range(50)])

    result = patterncheck.check(
        db,
        ["/feeds/#/comments/default", r"/feeds/[0-9]+/comments/default"],
        site_id=site.id,
    )
    counts = {r["pattern"]: r["count"] for r in result["results"]}
    # The shape, pasted as a regex: valid, saved, and inert.
    assert counts["/feeds/#/comments/default"] == 0
    # The pattern that means what they meant.
    assert counts[r"/feeds/[0-9]+/comments/default"] == 50
    assert result["checked"] == 50


def test_examples_come_back_so_a_count_can_be_believed(db: Session, settings: Settings) -> None:
    site = site_service.create_site(db, settings, seed_url=f"{BLOG}/", folder_id=1)
    capture = _capture(db, site.id)
    _urls(db, capture.id, [f"{BLOG}/feeds/{n}/comments/default" for n in range(10)])

    hit = patterncheck.check(db, [r"/comments/default"], site_id=site.id)["results"][0]
    assert hit["count"] == 10
    assert len(hit["examples"]) == patterncheck.EXAMPLES
    assert all("/comments/default" in url for url in hit["examples"])


def test_an_uncompilable_pattern_is_reported_not_raised(db: Session) -> None:
    """A site's own patterns are only validated when the whole scope saves, so
    a bad one can be sitting in the list. It must not cost the other counts."""
    result = patterncheck.check(db, ["[unclosed", r"[?&]m=1"])
    assert result["results"][0]["error"]
    assert result["results"][1]["error"] is None


def test_one_sites_urls_do_not_count_toward_another(db: Session, settings: Settings) -> None:
    one = site_service.create_site(db, settings, seed_url="https://one.example/", folder_id=1)
    two = site_service.create_site(db, settings, seed_url="https://two.example/", folder_id=1)
    _urls(db, _capture(db, one.id).id, [f"https://one.example/x{n}.html" for n in range(5)])
    _urls(db, _capture(db, two.id).id, [f"https://two.example/x{n}.html" for n in range(9)])

    assert patterncheck.check(db, [r"\.html"], site_id=one.id)["results"][0]["count"] == 5
    assert patterncheck.check(db, [r"\.html"], site_id=two.id)["results"][0]["count"] == 9
    # No site named: the instance-wide list is asked about the whole archive.
    assert patterncheck.check(db, [r"\.html"])["results"][0]["count"] == 14


def test_an_empty_archive_answers_rather_than_failing(db: Session) -> None:
    result = patterncheck.check(db, [r"[?&]m=1"])
    assert result["checked"] == 0
    assert result["captures"] == 0
    assert result["results"][0]["count"] == 0


# ── through the API ──────────────────────────────────────────────────────


def test_check_is_reachable_and_scoped(authed: TestClient) -> None:
    created = authed.post("/api/sites", json={"seed_url": f"{BLOG}/"}, headers=XHR)
    site_id = created.json()["id"]
    response = authed.post(
        "/api/crawl/skip-patterns/check",
        json={"patterns": ["/feeds/#/comments/default"], "site_id": site_id},
        headers=XHR,
    )
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["count"] == 0


def test_a_shape_row_can_be_skipped_on_this_site(authed: TestClient) -> None:
    created = authed.post("/api/sites", json={"seed_url": f"{BLOG}/"}, headers=XHR)
    site_id = created.json()["id"]
    pattern = r"^https?://[^/]+/feeds/[0-9]+/comments/default/?(?:$|\?)"

    response = authed.post(
        f"/api/sites/{site_id}/scope/skip", json={"pattern": pattern}, headers=XHR
    )
    assert response.status_code == 200, response.text
    assert response.json()["reject_patterns"] == [pattern]
    # Not the global list.
    assert response.json()["global_reject_patterns"] == []


def test_skipping_the_same_row_twice_adds_one_pattern(authed: TestClient) -> None:
    """The report is read top-down and a row can be clicked again before the
    list redraws."""
    created = authed.post("/api/sites", json={"seed_url": f"{BLOG}/"}, headers=XHR)
    site_id = created.json()["id"]
    for _ in range(2):
        response = authed.post(
            f"/api/sites/{site_id}/scope/skip", json={"pattern": r"[?&]m=1"}, headers=XHR
        )
    assert response.json()["reject_patterns"] == [r"[?&]m=1"]


def test_a_shape_row_can_be_skipped_everywhere(authed: TestClient, db: Session) -> None:
    created = authed.post("/api/sites", json={"seed_url": f"{BLOG}/"}, headers=XHR)
    site_id = created.json()["id"]

    response = authed.post(
        f"/api/sites/{site_id}/scope/skip",
        json={"pattern": r"[?&]m=1", "everywhere": True},
        headers=XHR,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["global_reject_patterns"] == [r"[?&]m=1"]
    # And it stayed out of the site's own, or removing it globally later would
    # not remove it here.
    assert body["reject_patterns"] == []
    assert skiplist.load(db) == [r"[?&]m=1"]


def test_a_pattern_that_will_not_compile_is_refused(authed: TestClient) -> None:
    created = authed.post("/api/sites", json={"seed_url": f"{BLOG}/"}, headers=XHR)
    site_id = created.json()["id"]
    response = authed.post(
        f"/api/sites/{site_id}/scope/skip", json={"pattern": "[unclosed"}, headers=XHR
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_pattern"


def test_skipping_does_not_disturb_the_rest_of_the_scope(
    authed: TestClient, db: Session, settings: Settings
) -> None:
    created = authed.post("/api/sites", json={"seed_url": f"{BLOG}/"}, headers=XHR)
    site_id = created.json()["id"]
    site = site_service.get_site(db, site_id)
    assert site is not None
    site_service.save_scope(
        db,
        site,
        Scope(
            seeds=[f"{BLOG}/"],
            hosts=[
                HostRule("b.blogspot.com", crawl_pages=True, fetch_assets=True),
                HostRule("1.bp.blogspot.com", crawl_pages=False, fetch_assets=True),
            ],
            reject_patterns=[r"[?&]replytocom="],
            max_pages=5000,
        ),
    )
    db.commit()

    body = authed.post(
        f"/api/sites/{site_id}/scope/skip", json={"pattern": r"[?&]m=1"}, headers=XHR
    ).json()
    assert body["reject_patterns"] == [r"[?&]replytocom=", r"[?&]m=1"]
    assert [h["host"] for h in body["hosts"]] == ["b.blogspot.com", "1.bp.blogspot.com"]
    assert body["max_pages"] == 5000
