"""Clearing finished jobs (docs/09).

A run of failures is what the jobs list looks like while something is being
got working — the browsertrix socket problem left a column of them — and there
was no way to remove any of it. The list is a record of work, so the only rule
that really matters is that tidying it never touches work still in progress.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, Job, Site
from cairn.db.types import utcnow
from tests.conftest import XHR


def make_job(db: Session, status: str, *, kind: str = "capture", site_id: int | None = None) -> int:
    job = Job(
        type=kind,
        site_id=site_id,
        status=status,
        spec={},
        queued_at=utcnow(),
        finished_at=utcnow() if status not in ("queued", "running") else None,
    )
    db.add(job)
    db.commit()
    return job.id


def ids(client: TestClient) -> set[int]:
    return {j["id"] for j in client.get("/api/jobs", headers=XHR).json()["items"]}


# ── one at a time ────────────────────────────────────────────────────────


def test_a_finished_job_can_be_deleted(authed: TestClient, db: Session) -> None:
    job_id = make_job(db, "failed")
    assert authed.delete(f"/api/jobs/{job_id}", headers=XHR).status_code == 200
    assert job_id not in ids(authed)


def test_a_running_job_cannot_be_deleted(authed: TestClient, db: Session) -> None:
    """The row is how the supervisor is reached. Deleting it would leave a
    crawler running against somebody's site with nothing able to stop it."""
    job_id = make_job(db, "running")
    refused = authed.delete(f"/api/jobs/{job_id}", headers=XHR)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "job_is_active"
    assert job_id in ids(authed)


def test_a_queued_job_cannot_be_deleted(authed: TestClient, db: Session) -> None:
    job_id = make_job(db, "queued")
    assert authed.delete(f"/api/jobs/{job_id}", headers=XHR).status_code == 409


def test_deleting_a_job_that_is_gone_is_a_404(authed: TestClient) -> None:
    assert authed.delete("/api/jobs/9999", headers=XHR).status_code == 404


# ── in bulk ──────────────────────────────────────────────────────────────


def test_clearing_by_status_leaves_the_others(authed: TestClient, db: Session) -> None:
    failed = [make_job(db, "failed") for _ in range(3)]
    ok = make_job(db, "ok")

    result = authed.post("/api/jobs/clear", json={"status": "failed"}, headers=XHR)
    assert result.status_code == 200
    assert result.json()["deleted"] == 3

    remaining = ids(authed)
    assert remaining == {ok}
    assert not remaining & set(failed)


def test_clearing_everything_finished_spares_the_active(authed: TestClient, db: Session) -> None:
    """The guard is on the delete, not on the caller remembering to filter."""
    make_job(db, "failed")
    make_job(db, "ok")
    make_job(db, "cancelled")
    running = make_job(db, "running")
    queued = make_job(db, "queued")

    result = authed.post("/api/jobs/clear", json={}, headers=XHR)
    assert result.json()["deleted"] == 3
    assert ids(authed) == {running, queued}


def test_clearing_an_active_status_is_refused_rather_than_silently_empty(
    authed: TestClient, db: Session
) -> None:
    """Asking to clear running jobs must not answer "deleted: 0" — that reads
    as "there were none", which is a different and reassuring fact."""
    make_job(db, "running")
    refused = authed.post("/api/jobs/clear", json={"status": "running"}, headers=XHR)
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "job_is_active"


def test_clearing_can_be_narrowed_to_one_site(authed: TestClient, db: Session) -> None:
    site = Site(
        slug="blog", title="Blog", seed_url="https://blog.test/", primary_host="blog.test",
        folder_id=1, archive_path="Unfiled/blog",
    )  # fmt: skip
    other = Site(
        slug="other", title="Other", seed_url="https://other.test/", primary_host="other.test",
        folder_id=1, archive_path="Unfiled/other",
    )  # fmt: skip
    db.add_all([site, other])
    db.commit()

    mine = make_job(db, "failed", site_id=site.id)
    theirs = make_job(db, "failed", site_id=other.id)

    result = authed.post("/api/jobs/clear", json={"site_id": site.id}, headers=XHR)
    assert result.json()["deleted"] == 1
    remaining = ids(authed)
    assert theirs in remaining
    assert mine not in remaining


def test_clearing_can_be_narrowed_to_one_type(authed: TestClient, db: Session) -> None:
    capture = make_job(db, "failed", kind="capture")
    discovery = make_job(db, "failed", kind="discovery")

    authed.post("/api/jobs/clear", json={"type": "discovery"}, headers=XHR)
    remaining = ids(authed)
    assert capture in remaining
    assert discovery not in remaining


# ── what must survive ────────────────────────────────────────────────────


def test_a_capture_outlives_the_job_that_made_it(authed: TestClient, db: Session) -> None:
    """The whole reason this is safe to offer.

    `captures.job_id` is ON DELETE SET NULL and the connection runs with
    `PRAGMA foreign_keys=ON`, so clearing the list drops the link and not the
    archive. Without the pragma the FK would be decoration and this would
    leave rows pointing at a job that no longer exists.
    """
    site = Site(
        slug="kept", title="Kept", seed_url="https://kept.test/", primary_host="kept.test",
        folder_id=1, archive_path="Unfiled/kept",
    )  # fmt: skip
    db.add(site)
    db.commit()
    job_id = make_job(db, "ok", site_id=site.id)
    capture = Capture(
        site_id=site.id, job_id=job_id, kind="full", engine_id="wget-warc",
        dir_name="20260814-1", status="ok", started_at=utcnow(),
    )  # fmt: skip
    db.add(capture)
    db.commit()
    capture_id = capture.id

    assert authed.post("/api/jobs/clear", json={}, headers=XHR).json()["deleted"] == 1

    db.expire_all()
    kept = db.get(Capture, capture_id)
    assert kept is not None, "clearing the job list must not delete archives"
    assert kept.job_id is None


def test_clearing_is_audited(authed: TestClient, db: Session) -> None:
    """A bulk delete that leaves no trace is not something to ship."""
    make_job(db, "failed")
    authed.post("/api/jobs/clear", json={"status": "failed"}, headers=XHR)

    entries = authed.get("/api/audit", headers=XHR).json()
    actions = [e["action"] for e in entries.get("items", entries)]
    assert "job.clear" in actions


# ── the projection's units ───────────────────────────────────────────────


def test_the_projection_says_what_each_number_counts(authed: TestClient, db: Session) -> None:
    """The index counts pages a site publishes. wget's live counter is URLs and
    browsertrix's is pages, so a bare ratio compares two different quantities —
    which is how "the index found 38,000 and the crawl is past 140,000" read as
    a runaway rather than as a unit mismatch.
    """
    job_id = make_job(db, "running")
    job = db.get(Job, job_id)
    assert job is not None
    job.progress = {"done": 51, "unit": "pages"}
    db.commit()

    data = authed.get(f"/api/jobs/{job_id}/projection", headers=XHR).json()
    assert data["counts"] == "pages"
    assert data["index_counts"] == "pages"


def test_a_job_with_no_unit_is_reported_as_urls(authed: TestClient, db: Session) -> None:
    """wget predates the field and counts URLs, so absence is not unknown."""
    job_id = make_job(db, "running")
    job = db.get(Job, job_id)
    assert job is not None
    job.progress = {"done": 900}
    db.commit()
    assert authed.get(f"/api/jobs/{job_id}/projection", headers=XHR).json()["counts"] == "urls"


def test_unlisted_paths_are_empty_while_robots_is_obeyed(authed: TestClient, db: Session) -> None:
    """They are only an explanation when they are actually in scope. Listing
    them regardless would blame robots.txt for a crawl it is constraining."""
    site = Site(
        slug="polite", title="Polite", seed_url="https://polite.test/",
        primary_host="polite.test", folder_id=1, archive_path="Unfiled/polite",
    )  # fmt: skip
    db.add(site)
    db.commit()
    job_id = make_job(db, "running", site_id=site.id)

    data = authed.get(f"/api/jobs/{job_id}/projection", headers=XHR).json()
    assert data["unlisted_paths"] == []


# ── pausing, and who is allowed to ───────────────────────────────────────


def _site(db: Session, *, engine_id: str, slug: str = "pausable") -> Site:
    site = Site(
        slug=slug, title=slug.title(), seed_url=f"https://{slug}.test/",
        primary_host=f"{slug}.test", folder_id=1, archive_path=f"Unfiled/{slug}",
        engine_id=engine_id,
    )  # fmt: skip
    db.add(site)
    db.commit()
    return site


def test_pause_is_refused_for_an_engine_that_cannot_resume(authed: TestClient, db: Session) -> None:
    """wget has no crawl-state serialisation, so pausing it would throw the
    work away while calling it a pause. Refused with the reason rather than
    accepted and quietly downgraded to a cancel."""
    site = _site(db, engine_id="wget-warc", slug="plainwget")
    job_id = make_job(db, "running", site_id=site.id)

    res = authed.post(f"/api/jobs/{job_id}/pause", headers=XHR)

    assert res.status_code == 409
    body = res.json()["error"]
    assert body["code"] == "not_pausable"
    assert "cannot resume" in body["message"]
    assert "Cancel it instead" in body["message"]


def test_a_queued_job_cannot_be_paused(authed: TestClient, db: Session) -> None:
    """There is nothing to continue from — it never started."""
    site = _site(db, engine_id="browsertrix", slug="queuedone")
    job_id = make_job(db, "queued", site_id=site.id)

    res = authed.post(f"/api/jobs/{job_id}/pause", headers=XHR)

    assert res.status_code == 409
    assert "Cancel it instead" in res.json()["error"]["message"]


def test_only_a_running_job_on_a_resumable_engine_offers_pause(
    authed: TestClient, db: Session
) -> None:
    """`can_pause` is what puts the button on screen, and the browser knows
    nothing about engines — so the server has to answer it."""
    btrix = _site(db, engine_id="browsertrix", slug="btrixsite")
    wget = _site(db, engine_id="wget-warc", slug="wgetsite")
    running = make_job(db, "running", site_id=btrix.id)
    other = make_job(db, "running", site_id=wget.id)
    done = make_job(db, "ok", site_id=btrix.id)

    by_id = {j["id"]: j for j in authed.get("/api/jobs", headers=XHR).json()["items"]}

    assert by_id[running]["can_pause"] is True
    assert by_id[other]["can_pause"] is False, "wget cannot resume"
    assert by_id[done]["can_pause"] is False, "and a finished job has nothing to pause"


def test_resuming_a_capture_that_is_not_paused_is_refused(authed: TestClient, db: Session) -> None:
    from cairn.db.models import Capture

    site = _site(db, engine_id="browsertrix", slug="finished")
    capture = Capture(
        site_id=site.id, kind="full", engine_id="browsertrix",
        dir_name="20260815T090000Z-full-browsertrix", status="ok", started_at=utcnow(),
    )  # fmt: skip
    db.add(capture)
    db.commit()

    res = authed.post(f"/api/captures/{capture.id}/resume", headers=XHR)

    assert res.status_code == 409
    assert res.json()["error"]["code"] == "not_paused"


def test_resuming_without_a_state_file_is_refused_rather_than_silently_recrawling(
    authed: TestClient, db: Session
) -> None:
    """The state file is what makes a resume a resume. Without it the job
    would run as a full re-crawl of a site somebody thought they were
    finishing — which costs bandwidth and looks like success."""
    from cairn.db.models import Capture

    site = _site(db, engine_id="browsertrix", slug="stateless")
    capture = Capture(
        site_id=site.id, kind="full", engine_id="browsertrix",
        dir_name="20260815T090000Z-full-browsertrix", status="paused", started_at=utcnow(),
    )  # fmt: skip
    db.add(capture)
    db.commit()

    res = authed.post(f"/api/captures/{capture.id}/resume", headers=XHR)

    assert res.status_code == 409
    assert res.json()["error"]["code"] == "no_resume_state"
    assert "start from the beginning" in res.json()["error"]["message"]


def test_a_companion_pass_on_a_resumable_site_cannot_be_paused(
    authed: TestClient, db: Session
) -> None:
    """The Blogger pagination pass runs wget whatever the site is set to.

    Asked of the site's engine, a browsertrix site offered Pause on it, and the
    click landed as an ordinary stop — the downgrade the endpoint refuses.
    """
    site = _site(db, engine_id="browsertrix", slug="leanblog")
    site.scope_settings = {"preset": "blogger-lean"}
    companion = Job(
        type="capture",
        site_id=site.id,
        status="running",
        spec={"kind": "companion"},
        queued_at=utcnow(),
    )
    db.add(companion)
    db.commit()
    crawl = make_job(db, "running", site_id=site.id)

    by_id = {j["id"]: j for j in authed.get("/api/jobs", headers=XHR).json()["items"]}
    assert by_id[companion.id]["can_pause"] is False
    assert by_id[crawl]["can_pause"] is True, "the site's own crawl still can"

    res = authed.post(f"/api/jobs/{companion.id}/pause", headers=XHR)
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "not_pausable"
    assert res.json()["error"]["message"].startswith("wget")


def test_once_a_job_has_a_capture_its_engine_is_the_one_that_says(
    authed: TestClient, db: Session
) -> None:
    site = _site(db, engine_id="browsertrix", slug="switched")
    capture = Capture(
        site_id=site.id, kind="full", engine_id="wget-warc",
        dir_name="20260815T090000Z-full-wget", status="running", started_at=utcnow(),
    )  # fmt: skip
    db.add(capture)
    db.flush()
    job = Job(
        type="capture",
        site_id=site.id,
        status="running",
        spec={"kind": "full", "capture_id": capture.id},
        queued_at=utcnow(),
    )
    db.add(job)
    db.commit()

    by_id = {j["id"]: j for j in authed.get("/api/jobs", headers=XHR).json()["items"]}
    assert by_id[job.id]["can_pause"] is False


# ── what a resume asks for ───────────────────────────────────────────────

FEED_REQUEST = {
    "kind": "feed",
    "feed_id": 3,
    "item_ids": [7],
    "extra_seeds": ["https://paused.test/2026/09/post.html"],
    "only_extra_seeds": True,
}


def _paused(
    db: Session,
    site: Site,
    *,
    kind: str = "feed",
    request: dict[str, object] | None = None,
    job_spec: dict[str, object] | None = None,
) -> Capture:
    job_id = None
    if job_spec is not None:
        job = Job(
            type="capture", site_id=site.id, status="ok", spec=job_spec,
            queued_at=utcnow(), finished_at=utcnow(),
        )  # fmt: skip
        db.add(job)
        db.flush()
        job_id = job.id
    capture = Capture(
        site_id=site.id, job_id=job_id, kind=kind, engine_id="browsertrix",
        dir_name=f"20260815T090000Z-{kind}-browsertrix", status="paused",
        started_at=utcnow(), request=request,
    )  # fmt: skip
    db.add(capture)
    db.commit()
    return capture


def test_a_capture_keeps_what_it_was_asked_for(db: Session) -> None:
    from cairn.services.jobs import recorded_request

    capture = _paused(db, _site(db, engine_id="browsertrix", slug="kept"), request=FEED_REQUEST)
    assert recorded_request(db, capture) == FEED_REQUEST


def test_an_older_capture_is_read_off_its_job_while_the_job_exists(db: Session) -> None:
    """Paused before captures kept a request. Its job still says what it was,
    until somebody clears the job list."""
    from cairn.services.jobs import recorded_request

    site = _site(db, engine_id="browsertrix", slug="older")
    spec = {**FEED_REQUEST, "capture_id": 1, "dir_name": "20260815T090000Z-feed-browsertrix"}
    assert recorded_request(db, _paused(db, site, job_spec=spec)) == FEED_REQUEST


def test_with_nothing_written_down_only_a_crawl_of_the_site_can_continue(db: Session) -> None:
    from cairn.services.jobs import recorded_request

    for kind, expected in (("full", {"kind": "full"}), ("feed", None), ("incremental", None)):
        site = _site(db, engine_id="browsertrix", slug=f"bare{kind}")
        assert recorded_request(db, _paused(db, site, kind=kind)) == expected, kind


def test_a_resume_that_carried_no_request_says_nothing_about_its_capture(db: Session) -> None:
    """A capture resumed before resumes carried a request ran as a crawl of the
    site, and its last job no longer says what it was for."""
    from cairn.services.jobs import recorded_request

    site = _site(db, engine_id="browsertrix", slug="resumedonce")
    capture = _paused(db, site, job_spec={"kind": "resume", "resume_capture_id": 99})
    assert recorded_request(db, capture) is None


def test_a_resume_asks_for_what_its_capture_was_asked_for(db: Session) -> None:
    from cairn.services.jobs import _asked

    carried = Job(spec={"kind": "resume", "resume_capture_id": 5, "request": FEED_REQUEST})
    assert _asked(db, carried, None) == FEED_REQUEST, "and still does if the capture is gone"
    kept_full = Capture(kind="full", request={"kind": "full"})
    assert _asked(db, carried, kept_full) == FEED_REQUEST, "the job's copy first"

    queued_before = Job(spec={"kind": "resume", "resume_capture_id": 5})
    assert _asked(db, queued_before, Capture(kind="feed", request=FEED_REQUEST)) == FEED_REQUEST
    assert _asked(db, queued_before, None) == queued_before.spec, "a crawl of the site, as before"

    plain = Job(spec={"kind": "full", "extra_seeds": []})
    assert _asked(db, plain, Capture(kind="feed", request=FEED_REQUEST)) == plain.spec


def test_a_resume_queued_before_requests_existed_still_resumes_as_its_capture(
    authed: TestClient, db: Session, tmp_path: Path
) -> None:
    """Paused before captures kept a request, and resumed by a job queued
    before jobs carried one — while the old job still said what it was for.
    Prepared as the posts it was, and the request kept on the capture from
    then on, so the next pause does not depend on that job either."""
    site = _site(db, engine_id="wget-warc", slug="upgraded")
    spec = {**FEED_REQUEST, "capture_id": 1, "dir_name": "20260815T090000Z-feed-browsertrix"}
    capture = _paused(db, site, job_spec=spec)
    resume = Job(
        type="capture",
        site_id=site.id,
        status="running",
        spec={"kind": "resume", "resume_capture_id": capture.id},
        queued_at=utcnow(),
    )
    db.add(resume)
    db.commit()

    supervisor = authed.app.state.supervisor  # type: ignore[attr-defined]
    prepared = supervisor._prepare(resume.id, tmp_path / "job")

    assert prepared.capture_id == capture.id
    assert prepared.seeds == FEED_REQUEST["extra_seeds"]
    assert prepared.scope["max_depth"] == 0
    assert (prepared.feed_id, prepared.item_ids) == (3, [7])
    db.expire_all()
    assert db.get(Capture, capture.id).request == FEED_REQUEST  # type: ignore[union-attr]


def test_resuming_a_capture_nothing_describes_is_refused(
    authed: TestClient, db: Session, settings: Settings
) -> None:
    """Better than the alternative, which is a crawl of the whole site in the
    place of a handful of posts."""
    from cairn.engines.protocol import RESUME_STATE_FILE
    from cairn.services import storage

    site = _site(db, engine_id="browsertrix", slug="undescribed")
    capture = _paused(db, site, kind="feed")
    directory = storage.site_dir(settings, site.archive_path) / storage.CAPTURES_DIR
    (directory / capture.dir_name).mkdir(parents=True)
    (directory / capture.dir_name / RESUME_STATE_FILE).write_text("queued: []\n")

    res = authed.post(f"/api/captures/{capture.id}/resume", headers=XHR)

    assert res.status_code == 409
    error = res.json()["error"]
    assert error["code"] == "resume_unknown"
    assert "crawl the whole site" in error["message"]
    db.expire_all()
    assert not db.scalars(select(Job).where(Job.site_id == site.id)).all()


# ── cancelling at every stage ────────────────────────────────────────────
#
# Reported as "the cancel button sometimes doesn't work, will sometimes get
# stuck and not do anything at all". It was a class of gap, not one bug:
# cancelling *delivered* a signal to whatever existed at that instant, so
# every stage with nothing to signal swallowed it and the job carried on.
# Each test below is one of those stages.


def _supervisor(client: TestClient):
    return client.app.state.supervisor  # type: ignore[attr-defined]


async def _cancel(client: TestClient, job_id: int) -> bool:
    return await _supervisor(client).cancel(job_id)


def test_cancelling_a_queued_job_still_works(authed: TestClient, db: Session) -> None:
    """The path that always worked, pinned so the rest cannot break it."""
    import asyncio

    job_id = make_job(db, "queued")
    assert asyncio.run(_cancel(authed, job_id)) is True

    db.expire_all()
    assert db.get(Job, job_id).status == "cancelled"


def test_a_job_claimed_but_not_yet_registered_records_the_cancel(
    authed: TestClient, db: Session
) -> None:
    """The claim window. `_claim` commits `running` on a worker thread, and
    until the dispatcher resumes the job is neither queued in the database nor
    present in `_running` — so the click had nowhere to land at all."""
    import asyncio

    from cairn.services.jobs import RunningJob

    supervisor = _supervisor(authed)
    job_id = make_job(db, "running")
    assert job_id not in supervisor._running

    assert asyncio.run(_cancel(authed, job_id)) is True
    assert job_id in supervisor._cancel_requests

    # And the dispatcher applies it the moment the job appears.
    running = RunningJob(job_id=job_id)
    assert supervisor._cancel_pending(running) is True
    assert running.cancelled is True
    # Consumed, not left to fire again at some later job.
    assert job_id not in supervisor._cancel_requests


def test_cancelling_before_anything_launched_is_not_silently_dropped(
    authed: TestClient, db: Session
) -> None:
    """The stage that caused the report: minutes of preparation — resolving
    scope, re-minting a profile, materializing a credential — during which
    `_stop` signals a process that is still None."""
    import asyncio

    from cairn.services.jobs import RunningJob

    supervisor = _supervisor(authed)
    job_id = make_job(db, "running")
    running = RunningJob(job_id=job_id)
    supervisor._running[job_id] = running
    try:
        assert running.process is None and running.container is None
        assert asyncio.run(_cancel(authed, job_id)) is True
        # Nothing to signal, so the flag is the whole record — and every
        # stage boundary reads it.
        assert running.cancelled is True
        assert supervisor._cancel_pending(running) is True
    finally:
        supervisor._running.pop(job_id, None)


def test_a_finished_job_reports_that_it_was_not_cancelled(authed: TestClient, db: Session) -> None:
    """The negative control. Returning True for a job it cannot touch is how
    a button reports success and changes nothing, which is the complaint."""
    import asyncio

    job_id = make_job(db, "ok")
    assert asyncio.run(_cancel(authed, job_id)) is False
    assert asyncio.run(_cancel(authed, 999999)) is False


def test_an_in_process_job_notices_through_its_progress_callback(
    authed: TestClient, db: Session, monkeypatch
) -> None:
    """Discovery and the maintenance jobs have no subprocess, so a signal has
    nowhere to go — the callback they already call is the only way in.

    Driven through the real `_run_discovery`, and the cancel lands *during*
    the crawl. Cancelling before `_run` would be caught by its first boundary
    check and this would pass without the callback ever being consulted —
    which is what the first draft of it did.
    """
    import asyncio

    from cairn.services.jobs import RunningJob

    supervisor = _supervisor(authed)
    site = _site(db, engine_id="wget-warc", slug="discoverme")
    job_id = make_job(db, "running", kind="discovery", site_id=site.id)
    running = RunningJob(job_id=job_id)
    supervisor._running[job_id] = running

    kept_going = False

    async def fake_discover(seed_url, options, progress):
        nonlocal kept_going
        progress("sampling", {"pages": 1})
        # The click, mid-crawl, through the real entry point.
        await supervisor.cancel(job_id)
        progress("sampling", {"pages": 2})
        kept_going = True
        return None

    monkeypatch.setattr("cairn.discovery.runner.discover", fake_discover)

    try:
        asyncio.run(supervisor._run(running))
    finally:
        supervisor._running.pop(job_id, None)

    assert not kept_going, "the crawl continued past a cancel"
    db.expire_all()
    job = db.get(Job, job_id)
    # Cancelled, not failed: the difference between "you stopped it" and
    # "something went wrong".
    assert job.status == "cancelled"
    assert job.error == "cancelled"


# ── what the pre-capture summary is built from ───────────────────────────


def test_site_detail_reports_the_engine_and_the_applied_preset(
    authed: TestClient, db: Session
) -> None:
    """The two fields the confirmation panel shows before a capture starts.

    Worth an HTTP test rather than only a unit one: the lookup can be right
    and the field still never reach the response, and a summary whose preset
    row is always "None" is worse than no summary — it would read as "this
    scope was built by hand" for every site on the instance.
    """
    site = _site(db, engine_id="browsertrix", slug="summarised")
    site.scope_settings = {"preset": "blogger"}
    db.commit()

    body = authed.get(f"/api/sites/{site.id}").json()

    assert body["engine_id"] == "browsertrix"
    assert body["preset"] == {"id": "blogger", "name": "Blogger / Blogspot"}


def test_a_hand_built_scope_reports_no_preset(authed: TestClient, db: Session) -> None:
    """Null is the honest answer and the UI renders it as one."""
    site = _site(db, engine_id="wget-warc", slug="byhand")
    site.scope_settings = {}
    db.commit()

    assert authed.get(f"/api/sites/{site.id}").json()["preset"] is None
