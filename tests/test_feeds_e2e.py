"""M6's exit criterion, run for real.

> A new post appears on a watched blog and is archived into that site's folder
> within the poll interval, at a fraction of a full capture's cost, with a
> notification.

Every clause is asserted against the thing itself: a real wget, a real WARC
read back with warcio, a real HTTP server that gains a post part-way through,
and a real socket receiving the notification. Needs wget, so it runs in the
container and in CI and skips elsewhere — the same arrangement as the capture
and replay suites.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, CaptureUrl, Feed, FeedItem, Job
from cairn.services import notify, settings_store
from tests.conftest import XHR, Blog

needs_wget = pytest.mark.skipif(shutil.which("wget") is None, reason="needs wget")


def _wait(client: TestClient, job_id: int, *, timeout: float = 120.0) -> dict[str, object]:
    """Block until a job leaves the queue, and return it."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}", headers=XHR).json()
        if job["status"] in ("ok", "failed", "cancelled", "interrupted"):
            return job
        time.sleep(0.4)
    raise AssertionError(f"job {job_id} never finished")


def _warc_text(settings: Settings, archive_path: str, dir_name: str) -> str:
    """Everything the capture actually stored, read back out of the WARC."""
    from warcio.archiveiterator import ArchiveIterator

    warc_dir = settings.archives_dir / archive_path / "captures" / dir_name / "warc"
    found: list[str] = []
    for warc in sorted(warc_dir.glob("*.warc.gz")):
        with warc.open("rb") as handle:
            for record in ArchiveIterator(handle):
                if record.rec_type == "response":
                    found.append(record.content_stream().read().decode("utf-8", errors="replace"))
    return "\n".join(found)


@needs_wget
def test_a_new_post_is_archived_into_the_sites_own_folder(
    authed: TestClient,
    db: Session,
    settings: Settings,
    blog: Blog,
    webhook: tuple[str, list[dict[str, object]]],
) -> None:
    hook_url, received = webhook
    notify.set_targets(db, [{"url": hook_url}])
    # Off by default because it is noisy; the milestone asks for it, so the
    # test asks for it.
    settings_store.put(db, notify.event_setting(notify.ITEMS_CAPTURED), True)
    db.commit()

    site = authed.post(
        "/api/sites", json={"seed_url": blog.base, "title": "Watched blog"}, headers=XHR
    ).json()
    site_id = site["id"]

    # ── a full capture first, so there is something to be incremental against
    full_job = authed.post(
        f"/api/sites/{site_id}/capture", json={"kind": "full"}, headers=XHR
    ).json()["job_id"]
    assert _wait(authed, full_job)["status"] == "ok"

    full = db.scalars(select(Capture).where(Capture.site_id == site_id)).one()
    assert full.url_count >= 4, "index, two posts and the logo at least"

    # ── watch the feed. The first poll is a baseline, not a backlog.
    feed_id = authed.post(
        f"/api/sites/{site_id}/feeds", json={"url": f"{blog.base}feed.xml"}, headers=XHR
    ).json()["id"]
    baseline = authed.post(f"/api/feeds/{feed_id}/poll", headers=XHR).json()
    assert baseline["baseline"] and baseline["job_ids"] == []

    # ── something is published
    blog.publish("post-3", "MARKER-THREE")

    polled = authed.post(f"/api/feeds/{feed_id}/poll", headers=XHR).json()
    assert polled["new_items"] == 1, polled
    assert len(polled["job_ids"]) == 1, "ten new posts would be one job, and so is one"
    feed_job = polled["job_ids"][0]
    assert _wait(authed, feed_job)["status"] == "ok"

    # ── into that site's folder, alongside the full capture
    db.expire_all()
    captures = db.scalars(
        select(Capture).where(Capture.site_id == site_id).order_by(Capture.started_at)
    ).all()
    assert len(captures) == 2
    incremental = captures[1]
    assert incremental.kind == "feed"

    site_dir = settings.archives_dir / site["archive_path"]
    capture_dir = site_dir / "captures" / incremental.dir_name
    assert capture_dir.is_dir(), "R12: it lands in the folder for that site"
    assert (site_dir / "captures" / full.dir_name).is_dir(), "beside the full capture, not instead"

    # ── with the new post's content actually in it
    text = _warc_text(settings, site["archive_path"], incremental.dir_name)
    assert "MARKER-THREE" in text

    # ── at a fraction of a full capture's cost
    #
    # Asserted on what wget was actually asked to fetch, read out of its own
    # crawl log. `url_count` is not the measure: a deduplicated URL produces a
    # revisit record and no CDX line, so counting records would call a capture
    # cheap precisely when it re-crawled everything and stored none of it.
    log = (capture_dir / "crawl.log").read_text(encoding="utf-8", errors="replace")
    requested = {
        line.split("URL:", 1)[1].split(" ", 1)[0].rsplit("/", 1)[-1]
        for line in log.splitlines()
        if "URL:" in line
    }
    assert "post-3.html" in requested
    assert "post-1.html" not in requested and "post-2.html" not in requested, (
        "an incremental capture must start from the new post alone; starting from "
        f"the site would re-crawl the archive: {sorted(requested)}"
    )
    assert incremental.bytes_written < full.bytes_written

    fetched = set(
        db.scalars(select(CaptureUrl.url).where(CaptureUrl.capture_id == incremental.id)).all()
    )
    assert any("post-3" in url for url in fetched)
    assert len(fetched) == len(requested), (
        "every URL wget fetched must appear in the capture's URL list, including the "
        f"ones it deduplicated: requested {sorted(requested)}, recorded {sorted(fetched)}"
    )

    # ── and the item is marked, pointing at the capture that holds it
    item = db.scalars(
        select(FeedItem).where(FeedItem.feed_id == feed_id, FeedItem.status == "captured")
    ).one()
    assert item.capture_id == incremental.id
    assert item.url.endswith("post-3.html")

    # ── with a notification
    titles = [str((entry["payload"] or {}).get("title", "")) for entry in received]  # type: ignore[union-attr]
    assert any("1 new item" in title for title in titles), titles


@needs_wget
def test_a_scheduler_tick_does_the_same_thing_unattended(
    authed: TestClient, db: Session, settings: Settings, blog: Blog
) -> None:
    """The button and the ticker have to be the same path, or what somebody
    verifies by hand is not what runs at three in the morning."""
    scheduler = authed.app.state.scheduler  # type: ignore[attr-defined]

    site_id = authed.post(
        "/api/sites", json={"seed_url": blog.base, "title": "Watched blog"}, headers=XHR
    ).json()["id"]
    feed_id = authed.post(
        f"/api/sites/{site_id}/feeds", json={"url": f"{blog.base}feed.xml"}, headers=XHR
    ).json()["id"]

    assert asyncio.run(scheduler.tick()).polled == 1, "a new feed is due immediately"
    blog.publish("post-3", "MARKER-THREE")

    # Due again. Nothing persists a fire time, so moving the row is all it
    # takes to make the next tick pick it up.
    feed = db.get(Feed, feed_id)
    assert feed is not None
    feed.next_poll_at = None
    db.commit()

    report = asyncio.run(scheduler.tick())

    assert report.polled == 1
    assert report.new_items == 1
    assert len(report.jobs) == 1
    assert _wait(authed, report.jobs[0])["status"] == "ok"
    text = _warc_text(
        settings,
        authed.get(f"/api/sites/{site_id}", headers=XHR).json()["archive_path"],
        db.scalars(
            select(Capture.dir_name)
            .where(Capture.site_id == site_id)
            .order_by(Capture.started_at.desc())
            .limit(1)
        ).one(),
    )
    assert "MARKER-THREE" in text


@needs_wget
def test_quiet_hours_defer_a_scheduled_capture_without_losing_it(
    authed: TestClient, db: Session, blog: Blog
) -> None:
    """Deferral is not a failure and not a loss: the items stay pending and the
    next tick inside the window picks them up."""
    from datetime import datetime, timedelta

    scheduler = authed.app.state.scheduler  # type: ignore[attr-defined]
    site_id = authed.post(
        "/api/sites", json={"seed_url": blog.base, "title": "Watched blog"}, headers=XHR
    ).json()["id"]
    feed_id = authed.post(
        f"/api/sites/{site_id}/feeds", json={"url": f"{blog.base}feed.xml"}, headers=XHR
    ).json()["id"]
    asyncio.run(scheduler.tick())

    # A window that is definitely not now, expressed in local time.
    now = datetime.now().astimezone()
    start = (now + timedelta(hours=2)).strftime("%H:%M")
    end = (now + timedelta(hours=4)).strftime("%H:%M")
    settings_store.put(db, "jobs.quiet_hours", {"enabled": True, "start": start, "end": end})
    db.commit()

    blog.publish("post-3", "MARKER-THREE")
    feed = db.get(Feed, feed_id)
    assert feed is not None
    feed.next_poll_at = None
    db.commit()

    report = asyncio.run(scheduler.tick())

    assert report.new_items == 1, "the poll still happens — it costs one request"
    assert report.jobs == [], "but nothing is captured"
    assert report.deferred == 1
    db.expire_all()
    assert db.scalar(select(Job.id).where(Job.site_id == site_id)) is None

    settings_store.put(db, "jobs.quiet_hours", {"enabled": False, "start": start, "end": end})
    db.commit()

    assert len(asyncio.run(scheduler.tick()).jobs) == 1, "picked up on the next tick"


@needs_wget
def test_a_capture_that_fails_leaves_the_item_pending(
    authed: TestClient, db: Session, blog: Blog
) -> None:
    """Marking an item captured because a job ran would leave a post
    permanently missing from the archive with nothing recording it was meant
    to be there."""
    scheduler = authed.app.state.scheduler  # type: ignore[attr-defined]
    supervisor = authed.app.state.supervisor  # type: ignore[attr-defined]

    site_id = authed.post(
        "/api/sites", json={"seed_url": blog.base, "title": "Watched blog"}, headers=XHR
    ).json()["id"]
    feed_id = authed.post(
        f"/api/sites/{site_id}/feeds", json={"url": f"{blog.base}feed.xml"}, headers=XHR
    ).json()["id"]
    asyncio.run(scheduler.tick())
    blog.publish("post-3", "MARKER-THREE")
    feed = db.get(Feed, feed_id)
    assert feed is not None
    feed.next_poll_at = None
    db.commit()

    report = asyncio.run(scheduler.tick())
    job_id = report.jobs[0]

    # Break the capture in the only way a test can do reliably: cancel it.
    asyncio.run(supervisor.cancel(job_id))
    _wait(authed, job_id)

    db.expire_all()
    item = db.scalars(select(FeedItem).where(FeedItem.feed_id == feed_id)).all()
    pending = [row for row in item if row.status == "pending"]
    assert len(pending) == 1, "the post is still owed"


def test_the_capture_directory_is_not_a_separate_archive(settings: Settings) -> None:
    """R12 in one line: there is no 'incremental archive' concept, and this
    guards the shape rather than the behaviour."""
    from cairn.services import storage

    assert storage.CAPTURES_DIR == "captures"
    assert not any(part.startswith("incremental") for part in Path(storage.CAPTURES_DIR).parts)
