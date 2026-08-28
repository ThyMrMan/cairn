"""A site served under both schemes, and the crawl that never ends.

Both halves come from one real capture: three days, 205,903 fetches for 2,732
distinct URLs, 192 complete laps alternating a full pass over `https://` with
the identical list over `http://`. Every page existed under both schemes;
every URL fetched exactly once was on an asset host reached under one.

`scheme_twin_reject_pattern` stops the cause. `crawlhealth.repetition` names
the symptom whatever the cause, because the next one will not be this.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, CaptureUrl, Job
from cairn.services import crawlhealth
from cairn.services import sites as site_service
from cairn.services.scope import (
    HostRule,
    Scope,
    build_reject_patterns,
    https_only_seed_hosts,
    scheme_twin_reject_pattern,
    to_wget_args,
)
from tests.conftest import XHR

BLOG = "changedforpleasure.blogspot.com"


def blog_scope(**kwargs: object) -> Scope:
    return Scope(
        seeds=[f"https://{BLOG}/"],
        hosts=[
            HostRule(BLOG, crawl_pages=True, fetch_assets=True),
            HostRule("blogger.googleusercontent.com", crawl_pages=False, fetch_assets=True),
        ],
        **kwargs,  # type: ignore[arg-type]
    )


# ── the reject that stops it ─────────────────────────────────────────────


def test_the_http_twin_of_an_https_seed_host_is_rejected() -> None:
    combined = re.compile("|".join(f"(?:{p})" for p in build_reject_patterns(blog_scope())))
    assert combined.search(f"http://{BLOG}/2014/05/another-freak.html")
    # And the https original is untouched, or the crawl archives nothing.
    assert not combined.search(f"https://{BLOG}/2014/05/another-freak.html")


def test_assets_on_other_hosts_keep_their_http_urls() -> None:
    """Scoped to the host. In the real capture six `http://` URLs lived on
    other hosts — fonts, two Blogger logos, two thumbnails — and none of them
    was part of the loop."""
    combined = re.compile("|".join(f"(?:{p})" for p in build_reject_patterns(blog_scope())))
    for url in (
        "http://fonts.gstatic.com/s/arimo/v36/P5sfzZCDf9.ttf",
        "http://www.blogger.com/img/logo-16.png",
        "http://4.bp.blogspot.com/-x/s35/a.jpg",
    ):
        assert not combined.search(url), url


def test_a_site_seeded_over_http_is_left_alone() -> None:
    """The evidence is the seed. A site that answers on http is not a site
    whose http pages are duplicates, and rejecting them would archive nothing.
    """
    scope = Scope(
        seeds=[f"http://{BLOG}/"],
        hosts=[HostRule(BLOG, crawl_pages=True, fetch_assets=True)],
    )
    assert https_only_seed_hosts(scope) == []
    combined = "|".join(build_reject_patterns(scope))
    assert f"^http://{re.escape(BLOG)}" not in combined


def test_a_host_seeded_both_ways_is_left_alone() -> None:
    """Somebody who added both explicitly meant both."""
    scope = Scope(
        seeds=[f"https://{BLOG}/", f"http://{BLOG}/"],
        hosts=[HostRule(BLOG, crawl_pages=True, fetch_assets=True)],
    )
    assert https_only_seed_hosts(scope) == []


def test_a_host_reached_only_by_link_following_is_left_alone() -> None:
    """No seed names it, so nothing here has observed which scheme it answers
    on — and guessing would drop half of a host on the evidence of none."""
    scope = Scope(
        seeds=[f"https://{BLOG}/"],
        hosts=[
            HostRule(BLOG, crawl_pages=True, fetch_assets=True),
            HostRule("linked.example", crawl_pages=True, fetch_assets=True),
        ],
    )
    assert https_only_seed_hosts(scope) == [BLOG]


def test_every_seed_host_gets_one_when_a_site_spans_domains() -> None:
    scope = Scope(
        seeds=["https://old.example/", "https://new.example/"],
        hosts=[
            HostRule("old.example", crawl_pages=True, fetch_assets=True),
            HostRule("new.example", crawl_pages=True, fetch_assets=True),
        ],
    )
    assert https_only_seed_hosts(scope) == ["new.example", "old.example"]


def test_the_pattern_tolerates_a_port_and_quotes_the_host() -> None:
    pattern = re.compile(scheme_twin_reject_pattern("my.blog"))
    assert pattern.search("http://my.blog:8080/x")
    # `.` is a regex wildcard until it is quoted, and a host arrives from
    # discovery and from user input.
    assert not pattern.search("http://myxblog/x")


def test_the_translation_tells_the_user_it_did_this() -> None:
    """A skip nobody was told about is indistinguishable from a bug, and this
    one is applied without being asked for."""
    notes = " ".join(to_wget_args(blog_scope()).notes)
    assert BLOG in notes
    assert "http" in notes


def test_the_reject_is_generated_not_stored_on_the_site(db: Session, settings: Settings) -> None:
    """It must not appear in the site's own list, or removing it would be
    impossible and re-adding it would double it."""
    site = site_service.create_site(db, settings, seed_url=f"https://{BLOG}/", folder_id=1)
    site_service.save_scope(db, site, blog_scope(reject_patterns=[r"[?&]m=1"]))
    scope = site_service.resolved_scope(db, site)
    assert scope.reject_patterns == [r"[?&]m=1"]
    assert any("^http://" in p for p in build_reject_patterns(scope))


def test_both_engines_still_enforce_one_boundary(tmp_path: Path) -> None:
    """A crawl has one boundary whichever engine walks it (docs/05). A reject
    added to the generated set has to reach both or they disagree."""
    from cairn.engines.browsertrix import Runner
    from cairn.engines.protocol import EventWriter, JobSpec

    spec = JobSpec.model_validate(
        {
            "protocol": "cairn.engine/v1",
            "job_id": 1,
            "site": {"id": 1, "slug": "b", "title": "B"},
            "output_dir": str(tmp_path / "o"),
            "temp_dir": str(tmp_path / "t"),
            "seeds": [f"https://{BLOG}/"],
            "scope": blog_scope().to_dict(),
            "config": {},
        }
    )
    argv = Runner(spec, EventWriter())._argv()
    blocked = argv[argv.index("--blockRules") + 1]
    assert re.search(blocked, f"http://{BLOG}/p.html")


# ── the check that names it whatever the cause ───────────────────────────


def _capture(db: Session, site_id: int, job_id: int | None = None) -> Capture:
    capture = Capture(
        site_id=site_id,
        job_id=job_id,
        kind="full",
        engine_id="wget-warc",
        dir_name="c1",
        status="running",
    )
    db.add(capture)
    db.flush()
    return capture


def _fetch(db: Session, capture_id: int, urls: list[str]) -> None:
    for url in urls:
        db.add(CaptureUrl(capture_id=capture_id, url=url, host=BLOG, status_code=200))
    db.flush()


def test_a_healthy_crawl_is_not_reported_as_looping(db: Session, settings: Settings) -> None:
    site = site_service.create_site(db, settings, seed_url=f"https://{BLOG}/", folder_id=1)
    capture = _capture(db, site.id)
    _fetch(db, capture.id, [f"https://{BLOG}/p{n}.html" for n in range(2000)])

    result = crawlhealth.repetition(db, capture.id)
    assert result.ratio == 1.0
    assert not result.looping
    assert result.worst == []


def test_the_real_shape_is_caught(db: Session, settings: Settings) -> None:
    """192 laps of one list, which is what the capture actually did."""
    site = site_service.create_site(db, settings, seed_url=f"https://{BLOG}/", folder_id=1)
    capture = _capture(db, site.id)
    pages = [f"https://{BLOG}/p{n}.html" for n in range(100)]
    twins = [f"http://{BLOG}/p{n}.html" for n in range(100)]
    for _lap in range(20):
        _fetch(db, capture.id, pages)
        _fetch(db, capture.id, twins)

    result = crawlhealth.repetition(db, capture.id)
    assert result.looping
    assert result.ratio >= crawlhealth.LOOP_RATIO
    # And it names the URLs, which is what made the diagnosis possible: every
    # one of them has a twin under the other scheme.
    assert result.worst
    assert all(hit["count"] > 1 for hit in result.worst)


def test_a_bounded_duplication_is_not_called_a_loop(db: Session, settings: Settings) -> None:
    """Two URLs mapping to one file cost exactly one extra fetch each —
    measured at 2.0x on 6, 30 and 90 pages, flat. Real, bounded, and not worth
    an alarm that would then be ignored."""
    site = site_service.create_site(db, settings, seed_url=f"https://{BLOG}/", folder_id=1)
    capture = _capture(db, site.id)
    pages = [f"https://{BLOG}/p{n}.html" for n in range(1000)]
    _fetch(db, capture.id, pages)
    _fetch(db, capture.id, pages)

    result = crawlhealth.repetition(db, capture.id)
    assert result.ratio == 2.0
    assert not result.looping


def test_a_tiny_crawl_cannot_trip_it(db: Session, settings: Settings) -> None:
    """A four-page site whose favicon was fetched twice is not looping, and an
    alarm in the first seconds of every job is an alarm nobody reads."""
    site = site_service.create_site(db, settings, seed_url=f"https://{BLOG}/", folder_id=1)
    capture = _capture(db, site.id)
    _fetch(db, capture.id, [f"https://{BLOG}/favicon.ico"] * 40)

    result = crawlhealth.repetition(db, capture.id)
    assert result.ratio == 40.0
    assert not result.looping, "under MIN_ROWS there is not enough evidence"


def test_the_window_notices_a_crawl_that_starts_looping_late(
    db: Session, settings: Settings
) -> None:
    """The property the window exists for. Over the whole capture this reads
    about 1.6x and says nothing; over the recent rows it is unmistakable."""
    site = site_service.create_site(db, settings, seed_url=f"https://{BLOG}/", folder_id=1)
    capture = _capture(db, site.id)
    _fetch(db, capture.id, [f"https://{BLOG}/first-{n}.html" for n in range(15000)])
    loop = [f"https://{BLOG}/loop{n}.html" for n in range(300)]
    for _lap in range(30):
        _fetch(db, capture.id, loop)

    assert crawlhealth.repetition(db, capture.id, window=5_000).looping


def test_an_empty_capture_answers_rather_than_dividing_by_zero(
    db: Session, settings: Settings
) -> None:
    site = site_service.create_site(db, settings, seed_url=f"https://{BLOG}/", folder_id=1)
    capture = _capture(db, site.id)
    result = crawlhealth.repetition(db, capture.id)
    assert (result.checked, result.distinct, result.ratio, result.looping) == (0, 0, 0.0, False)


@pytest.mark.parametrize("with_capture", [True, False])
def test_the_projection_reports_it(
    authed: TestClient, db: Session, settings: Settings, with_capture: bool
) -> None:
    site = site_service.create_site(db, settings, seed_url=f"https://{BLOG}/", folder_id=1)
    job = Job(type="capture", site_id=site.id, status="running", spec={})
    db.add(job)
    db.flush()
    if with_capture:
        capture = _capture(db, site.id, job_id=job.id)
        pages = [f"https://{BLOG}/p{n}.html" for n in range(100)]
        for _lap in range(10):
            _fetch(db, capture.id, pages)
            _fetch(db, capture.id, [u.replace("https://", "http://") for u in pages])
    db.commit()

    body = authed.get(f"/api/jobs/{job.id}/projection", headers=XHR).json()
    if not with_capture:
        assert body["repetition"] is None
    else:
        assert body["repetition"]["looping"] is True
        assert body["repetition"]["ratio"] >= crawlhealth.LOOP_RATIO
