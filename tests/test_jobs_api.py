"""Clearing finished jobs (docs/09).

A run of failures is what the jobs list looks like while something is being
got working — the browsertrix socket problem left a column of them — and there
was no way to remove any of it. The list is a record of work, so the only rule
that really matters is that tidying it never touches work still in progress.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

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
