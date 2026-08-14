"""Grouping a capture's URLs by shape.

Written against the case that produced it: a Blogger crawl whose index found
38,000 posts and whose URL count went past 140,000, where counting guessed
substrings gave three numbers that were each roughly the size of the whole
archive and explained nothing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, CaptureUrl, Site
from cairn.db.types import utcnow
from cairn.services import storage, urlshapes

LABELS = [f"Label{n}" for n in range(60)]


def _capture(db: Session, settings: Settings, urls: list[str]) -> int:
    site = Site(
        folder_id=1,
        slug="blog",
        title="Blog",
        seed_url="http://blog.test/",
        primary_host="blog.test",
        archive_path="Unfiled/blog",
    )
    db.add(site)
    db.flush()
    storage.ensure_site_dirs(settings, site.archive_path)
    capture = Capture(
        site_id=site.id,
        kind="full",
        engine_id="wget-warc",
        dir_name="20260814-1",
        status="running",
        started_at=utcnow(),
    )
    db.add(capture)
    db.flush()
    for url in urls:
        db.add(CaptureUrl(capture_id=capture.id, url=url, host="blog.test", size_bytes=1000))
    db.commit()
    return capture.id


def _blogger_urls() -> list[str]:
    urls = ["http://blog.test/"]
    # 40 real posts, the thing somebody wanted archived.
    for n in range(40):
        urls.append(f"http://blog.test/2019/{n % 12 + 1:02d}/post-{n}.html")
        urls.append(f"http://blog.test/img/photo-{n}.jpg")
    # And the trap: every label, paginated. This is the shape that ate the
    # crawl, and no single one of these URLs looks unreasonable on its own.
    for label in LABELS:
        for page in range(8):
            urls.append(
                f"http://blog.test/search/label/{label}"
                f"?updated-max=2019-{page + 1:02d}-01T00:00:00-07:00&max-results=20"
            )
    return urls


def test_the_shape_that_ate_the_crawl_is_the_first_row(
    authed: TestClient, db: Session, settings: Settings
) -> None:
    capture_id = _capture(db, settings, _blogger_urls())

    body = authed.get(f"/api/captures/{capture_id}/url-shapes").json()
    top = body["shapes"][0]

    assert top["shape"] == "/search/label/*?max-results&updated-max"
    assert top["count"] == len(LABELS) * 8
    assert top["count"] > body["total"] / 2, "the trap should dominate the report"
    assert top["example"].startswith("http://blog.test/search/label/")


def test_sixty_labels_are_one_row_rather_than_sixty(
    authed: TestClient, db: Session, settings: Settings
) -> None:
    """The whole point of the two passes.

    Nothing about `/search/label/Travel` in isolation says the last segment is
    a value — it is a short word in a sensible position. Only the fact that
    sixty different ones appear at that depth says so, which a per-URL
    heuristic cannot see.
    """
    capture_id = _capture(db, settings, _blogger_urls())
    body = authed.get(f"/api/captures/{capture_id}/url-shapes").json()

    label_rows = [s for s in body["shapes"] if s["shape"].startswith("/search/label/")]
    assert len(label_rows) == 1, [s["shape"] for s in label_rows]


def test_the_posts_stay_legible_next_to_it(
    authed: TestClient, db: Session, settings: Settings
) -> None:
    """Dates collapse, extensions survive: `*.html` and `*.jpg` are different
    answers to "what is this crawl doing" and must not merge."""
    capture_id = _capture(db, settings, _blogger_urls())
    shapes = {
        s["shape"]: s["count"]
        for s in authed.get(f"/api/captures/{capture_id}/url-shapes").json()["shapes"]
    }

    assert shapes["/#/#/*.html"] == 40
    assert shapes["/img/*.jpg"] == 40


def test_every_url_is_counted_exactly_once(
    authed: TestClient, db: Session, settings: Settings
) -> None:
    urls = _blogger_urls()
    capture_id = _capture(db, settings, urls)
    body = authed.get(f"/api/captures/{capture_id}/url-shapes?limit=200").json()

    assert body["total"] == len(urls)
    assert sum(s["count"] for s in body["shapes"]) == len(urls)
    assert body["truncated"] is False


def test_a_capture_with_no_urls_is_not_an_error(
    authed: TestClient, db: Session, settings: Settings
) -> None:
    capture_id = _capture(db, settings, [])
    body = authed.get(f"/api/captures/{capture_id}/url-shapes").json()
    assert body == {"total": 0, "distinct_shapes": 0, "shapes": [], "truncated": False}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://h/2019/05/a.html", "/#/#/a.html"),
        ("http://h/", "/"),
        ("http://h/a/b/c", "/a/b/c"),
        ("http://h/p?b=2&a=1", "/p?a&b"),
        # Same keys, different values, one shape — which is the entire reason
        # the query is reduced to its key names.
        ("http://h/p?a=9&b=8", "/p?a&b"),
    ],
)
def test_shape_of_a_single_url(url: str, expected: str) -> None:
    """Learn first, then shape.

    Calling `shape_of` against an empty tree is not a smaller version of the
    real thing — a node the first pass never saw is treated as varying, on
    purpose, because the only way that happens for real is the node budget
    running out and a shorter report is the safe answer there.
    """
    tree: urlshapes.Tree = {}
    urlshapes.learn([url], tree)
    assert urlshapes.shape_of(url, tree) == expected


# ── the live projection ──────────────────────────────────────────────────


def test_a_running_crawl_reports_a_rate_and_its_distance_to_the_cap(
    authed: TestClient, db: Session, settings: Settings
) -> None:
    """No percentage, on purpose.

    A bar drawn against the index's page estimate read 370% on the crawl that
    prompted all of this. What can be said honestly is the rate and the cap.
    """
    from datetime import timedelta

    from cairn.db.models import Job

    _capture(db, settings, [])
    site_id = db.scalar(select(Site.id))
    job = Job(
        type="capture",
        site_id=site_id,
        status="running",
        queued_at=utcnow(),
        started_at=utcnow() - timedelta(minutes=10),
        progress={"done": 1200, "bytes": 5_000_000},
    )
    db.add(job)
    db.commit()

    body = authed.get(f"/api/jobs/{job.id}/projection").json()
    assert body["running"] is True
    assert body["urls"] == 1200
    # 1200 URLs in ten minutes.
    assert 110 <= body["per_minute"] <= 130
    assert "percent" not in body and "progress" not in body


def test_the_cap_distance_is_absent_rather_than_zero_when_unknowable(
    authed: TestClient, db: Session, settings: Settings
) -> None:
    from cairn.db.models import Job

    _capture(db, settings, [])
    site_id = db.scalar(select(Site.id))
    job = Job(
        type="capture",
        site_id=site_id,
        status="running",
        queued_at=utcnow(),
        started_at=utcnow(),
        progress={},
    )
    db.add(job)
    db.commit()

    body = authed.get(f"/api/jobs/{job.id}/projection").json()
    assert body["eta_to_cap_s"] is None, "a crawl with no rate yet must not claim 0s remaining"
    assert body["urls"] == 0
