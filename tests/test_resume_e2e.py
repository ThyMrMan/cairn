"""A paused capture is continued as the capture it was.

Only an engine that keeps its crawl state can be paused, and the one that
ships with that is browsertrix, which needs Docker. The engine here is a
stand-in that keeps the same contract: it writes down what it was handed,
waits to be stopped when told to, and on SIGTERM leaves a resume state and
says so. That is enough to drive the real supervisor, the real pause and
resume endpoints and the real feed machinery end to end.

What this pins, each found by reading the resume path rather than by a
report: a paused feed capture continued as a crawl of the whole site, its
feed items were never marked, and the pause itself was counted as a failed
capture — so the feed backed off and then captured the same posts again
beside the paused one.

POSIX only. On Windows SIGTERM is TerminateProcess, and no engine gets to
write its state.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, Feed, FeedItem, Job
from cairn.services import storage
from tests.conftest import XHR, Blog

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="SIGTERM cannot be handled on Windows"
)

ENGINE_ID = "resumable-stand-in"

# Written into the engine's directory; the first run that finds `hold` beside
# it waits to be paused, and every other run goes straight through.
ENGINE_PY = """
import io
import json
import signal
import sys
import time
from pathlib import Path

from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
out = Path(spec["output_dir"])
seed_file = Path(spec["temp_dir"]) / (spec.get("seed_file") or "seeds.txt")
if seed_file.is_file():
    seeds = seed_file.read_text(encoding="utf-8").split()
else:
    seeds = list(spec["seeds"])
state_file = (spec.get("resume") or {}).get("state_file")
resumed_from = Path(state_file).read_text(encoding="utf-8") if state_file else None

with (out / "runs.jsonl").open("a", encoding="utf-8") as ledger:
    ledger.write(json.dumps({
        "job": spec["job_id"],
        "seeds": seeds,
        "max_depth": spec["scope"].get("max_depth"),
        "resumed_from": resumed_from,
    }) + "\\n")

stopping = False


def stop(*_args):
    global stopping
    stopping = True


signal.signal(signal.SIGTERM, stop)


def emit(**event):
    print(json.dumps(event), flush=True)


emit(type="started")
warc = out / "warc" / f"part-{spec['job_id']}.warc.gz"
warc.parent.mkdir(parents=True, exist_ok=True)
with warc.open("wb") as handle:
    writer = WARCWriter(handle, gzip=True)
    for url in seeds:
        body = f"<html><body><h1>{url}</h1></body></html>".encode()
        headers = StatusAndHeaders("200 OK", [("Content-Type", "text/html")], protocol="HTTP/1.1")
        record = writer.create_warc_record(
            url, "response", payload=io.BytesIO(body), http_headers=headers
        )
        writer.write_record(record)
emit(type="artifact", kind="warc", path=f"warc/{warc.name}")
for url in seeds:
    emit(type="url", url=url, status=200, mime="text/html")

hold = Path(__file__).with_name("hold")
if resumed_from is None and hold.exists():
    hold.unlink()
    deadline = time.monotonic() + 60
    while not stopping and time.monotonic() < deadline:
        time.sleep(0.05)
    (out / "resume-state.yaml").write_text("queued: " + json.dumps(seeds) + "\\n")
    emit(type="result", status="partial", stats={"resumable": True})
else:
    emit(type="result", status="ok", stats={})
"""


def _install(settings: Settings, client: TestClient) -> Path:
    directory = settings.engines_dir / ENGINE_ID
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "apiVersion": "cairn.engine/v1",
        "id": ENGINE_ID,
        "name": "Resumable stand-in",
        "version": "0.1.0",
        "description": "Writes down what it was handed, and pauses on SIGTERM.",
        # This interpreter, which has warcio; a drop-in's command is run as written.
        "runtime": {"type": "subprocess", "command": [sys.executable, "engine.py"]},
        "capabilities": {
            "outputs": ["warc"],
            "javascript": False,
            "resumable": True,
            "incremental": False,
        },
        "config_schema": {"type": "object", "additionalProperties": False, "properties": {}},
    }
    # JSON is YAML, and needs no escaping for a Windows interpreter path.
    (directory / "engine.yaml").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "engine.py").write_text(ENGINE_PY, encoding="utf-8")
    assert client.post("/api/engines/rescan", headers=XHR).status_code == 200
    return directory


def _until(check: Callable[[], bool], what: str, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def _job(client: TestClient, job_id: int) -> dict[str, Any]:
    job: dict[str, Any] = client.get(f"/api/jobs/{job_id}", headers=XHR).json()
    return job


def _finished(client: TestClient, job_id: int) -> dict[str, Any]:
    ended = ("ok", "failed", "cancelled", "interrupted")
    _until(lambda: _job(client, job_id)["status"] in ended, f"job {job_id} to end")
    job = _job(client, job_id)
    assert job["status"] == "ok", job.get("error")
    return job


def _counts(client: TestClient, site_id: int) -> dict[str, int]:
    feeds = client.get(f"/api/sites/{site_id}/feeds", headers=XHR).json()
    counts: dict[str, int] = feeds[0]["counts"]
    return counts


def _runs(settings: Settings, archive_path: str, dir_name: str) -> list[dict[str, Any]]:
    ledger = storage.site_dir(settings, archive_path) / storage.CAPTURES_DIR / dir_name
    text = (ledger / "runs.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def _paused_feed_capture(
    authed: TestClient, db: Session, settings: Settings, blog: Blog
) -> tuple[dict[str, Any], int, Capture, str]:
    """A watched site, a new post, and the capture of it paused mid-crawl."""
    engine_dir = _install(settings, authed)
    site = authed.post(
        "/api/sites",
        json={"seed_url": blog.base, "title": "Paused blog", "engine_id": ENGINE_ID},
        headers=XHR,
    ).json()
    feed_id = authed.post(
        f"/api/sites/{site['id']}/feeds", json={"url": f"{blog.base}feed.xml"}, headers=XHR
    ).json()["id"]
    assert authed.post(f"/api/feeds/{feed_id}/poll", headers=XHR).json()["baseline"]

    blog.publish("post-3", "MARKER-THREE")
    (engine_dir / "hold").touch()
    polled = authed.post(f"/api/feeds/{feed_id}/poll", headers=XHR).json()
    assert polled["new_items"] == 1, polled
    job_id = polled["job_ids"][0]

    captures = storage.site_dir(settings, site["archive_path"]) / storage.CAPTURES_DIR

    def crawling() -> bool:
        # The engine's own word that it is running, rather than `can_pause`
        # alone: that turns true when the job is claimed, a moment before the
        # supervisor holds a process it could stop.
        db.expire_all()
        job = db.get(Job, job_id)
        dir_name = (job.spec or {}).get("dir_name") if job is not None else None
        return bool(dir_name) and (captures / str(dir_name) / "runs.jsonl").is_file()

    _until(crawling, "the engine to start")
    assert _job(authed, job_id)["can_pause"]
    assert authed.post(f"/api/jobs/{job_id}/pause", headers=XHR).status_code == 200
    _finished(authed, job_id)

    db.expire_all()
    capture = db.scalars(select(Capture).where(Capture.site_id == site["id"])).one()
    assert capture.status == "paused"
    return site, feed_id, capture, f"{blog.base}post-3.html"


def test_a_paused_feed_capture_is_resumed_as_itself(
    authed: TestClient, db: Session, settings: Settings, blog: Blog
) -> None:
    site, feed_id, capture, post = _paused_feed_capture(authed, db, settings, blog)
    item = db.scalars(select(FeedItem).where(FeedItem.url == post)).one()

    # What it was asked for is kept with it.
    assert capture.request == {
        "kind": "feed",
        "feed_id": feed_id,
        "item_ids": [item.id],
        "extra_seeds": [post],
        "only_extra_seeds": True,
    }

    # ── a pause is not a failure, and the post is not captured twice
    feed = db.get(Feed, feed_id)
    assert feed is not None
    assert (feed.capture_failures, feed.next_capture_at) == (0, None)
    assert item.status == "pending", "nothing has captured it yet"
    counts = _counts(authed, site["id"])
    assert (counts["pending"], counts["held"]) == (0, 1), counts
    scheduler = authed.app.state.scheduler  # type: ignore[attr-defined]
    assert asyncio.run(scheduler.tick()).jobs == [], "the paused capture has that post"

    # ── the job that knew what it was for is cleared from the list
    assert authed.post("/api/jobs/clear", json={}, headers=XHR).json()["deleted"] >= 1
    db.expire_all()
    assert db.get(Capture, capture.id).job_id is None  # type: ignore[union-attr]

    # ── and the capture is resumed anyway, as what it was
    resumed = authed.post(f"/api/captures/{capture.id}/resume", headers=XHR)
    assert resumed.status_code == 202, resumed.text
    resume_id = resumed.json()["job_id"]
    _finished(authed, resume_id)
    # The job carries the request as well, which is what a resume whose
    # capture has gone by the time it runs has to go on.
    resume_job = db.get(Job, resume_id)
    assert resume_job is not None
    assert resume_job.spec["request"] == capture.request

    first, second = _runs(settings, site["archive_path"], capture.dir_name)
    assert (first["seeds"], first["max_depth"], first["resumed_from"]) == ([post], 0, None)
    assert second["resumed_from"] is not None, "continued from its state, not started again"
    assert second["seeds"] == [post], "the post it was for, not the site"
    assert second["max_depth"] == 0

    db.expire_all()
    captures = db.scalars(select(Capture).where(Capture.site_id == site["id"])).all()
    assert [c.id for c in captures] == [capture.id], "continued into the same capture"
    assert captures[0].status == "ok"
    item = db.scalars(select(FeedItem).where(FeedItem.url == post)).one()
    assert (item.status, item.capture_id) == ("captured", capture.id)
    counts = _counts(authed, site["id"])
    assert (counts["pending"], counts["held"], counts["captured"]) == (0, 0, 1), counts


def test_a_resume_whose_capture_is_gone_captures_the_same_posts_afresh(
    authed: TestClient, db: Session, settings: Settings, blog: Blog
) -> None:
    """The documented fallback — a resume that finds its capture gone becomes a
    fresh capture — used to mean a fresh crawl of the whole site."""
    site, _feed_id, capture, post = _paused_feed_capture(authed, db, settings, blog)
    asked = capture.request
    paused_at = capture.started_at

    # Deleting the paused capture hands its post back to the feed at once:
    # nothing had to remember to release it.
    deleted = authed.delete(f"/api/captures/{capture.id}?force=true", headers=XHR)
    assert deleted.status_code == 200, deleted.text
    counts = _counts(authed, site["id"])
    assert (counts["pending"], counts["held"]) == (1, 0), counts

    # A resume queued before the delete, and run after it.
    supervisor = authed.app.state.supervisor  # type: ignore[attr-defined]
    job = supervisor.enqueue(
        db,
        job_type="capture",
        site_id=site["id"],
        spec={"kind": "resume", "resume_capture_id": capture.id, "request": asked},
    )
    db.commit()
    supervisor.notify()
    _finished(authed, job.id)

    db.expire_all()
    fresh = db.scalars(select(Capture).where(Capture.site_id == site["id"])).one()
    # Not by id or directory: SQLite hands a deleted row's id to the next one,
    # and a directory name has one-second resolution.
    assert fresh.started_at > paused_at, "a new capture, not the deleted one back"
    assert (fresh.kind, fresh.request) == ("feed", asked)
    (run,) = _runs(settings, site["archive_path"], fresh.dir_name)
    assert (run["seeds"], run["max_depth"]) == ([post], 0), "the same post, not the site"
    item = db.scalars(select(FeedItem).where(FeedItem.url == post)).one()
    assert (item.status, item.capture_id) == ("captured", fresh.id)
