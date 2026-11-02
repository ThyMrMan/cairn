"""Wayback pre-fill, stage 2: the import engine (docs/19).

Engine tests drive `Runner` in-process against `httpx.MockTransport`, with a
fake clock so archive.org's backoffs cost nothing. One end-to-end test runs
the real engine subprocess through the job queue against a fake archive.org on
localhost, then checks the capture replays — no test talks to archive.org.
"""

from __future__ import annotations

import gzip
import io
import json
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from warcio.archiveiterator import ArchiveIterator

from cairn.config import Settings
from cairn.db.models import Capture, Job, Site
from cairn.db.types import utcnow
from cairn.engines import wayback as engine
from cairn.engines.protocol import RESUME_STATE_FILE, JobSpec
from cairn.services import settings_store, storage
from cairn.services import wayback as wayback_service
from cairn.services.jobs import _host_for_job, merge_artifacts
from cairn.wayback import survey as wb
from tests.conftest import XHR

STORY = "https://www.fictionmania.tv/stories/readtextstory.html?storyID={}"
MEMENTO = "Tue, 05 Mar 2019 10:20:30 GMT"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def selection_line(
    url: str, ts: str, digest: str = "", older: list[list[str]] | None = None
) -> str:
    return json.dumps(
        {
            "key": url.lower(),
            "url": url,
            "kind": "page",
            "mime": "text/html",
            "ts": ts,
            "digest": digest,
            "older": older or [],
        }
    )


def write_selection(path: Path, lines: list[str]) -> Path:
    path.write_bytes(gzip.compress(("\n".join(lines) + "\n").encode()))
    return path


def archived(
    body: bytes,
    *,
    status: int = 200,
    gzip_body: bool = False,
    extra: dict[str, str] | None = None,
) -> Canned:
    payload = gzip.compress(body) if gzip_body else body
    headers = {
        "Content-Type": "text/html; charset=utf-8",
        "Memento-Datetime": MEMENTO,
        "X-Archive-Orig-Server": "Apache",
        "X-Archive-Orig-Date": MEMENTO,
        "X-Archive-Orig-Content-Length": "999",
        **({"Content-Encoding": "gzip"} if gzip_body else {}),
        **(extra or {}),
    }
    return (status, headers, payload)


Canned = tuple[int, dict[str, str], bytes]


def plain(status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> Canned:
    """archive.org speaking for itself: no Memento-Datetime, no originals."""
    return (status, headers or {}, body)


class FakeWayback:
    """`web/<ts>id_/<url>` → a canned response, recording every request."""

    def __init__(self) -> None:
        self.snapshots: dict[tuple[str, str], Canned] = {}
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        self.requests.append(path)
        rest = path.split("/web/", 1)[1]
        ts, url = rest.split("id_/", 1)
        canned = self.snapshots.get((ts, url))
        if canned is None:
            return httpx.Response(404, stream=httpx.ByteStream(b"not archived"))
        status, headers, body = canned
        # Streamed, as a real connection is, so raw reads work.
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


def make_runner(
    tmp_path: Path,
    fake: FakeWayback,
    lines: list[str],
    *,
    config: dict[str, Any] | None = None,
    max_duration_s: int | None = None,
    clock: Clock | None = None,
    wall: list[float] | None = None,
) -> tuple[engine.Runner, list[dict[str, Any]], Path]:
    out = tmp_path / "capture"
    tmp = tmp_path / "tmp"
    out.mkdir(exist_ok=True)
    tmp.mkdir(exist_ok=True)
    selection = write_selection(tmp / "selection.jsonl.gz", lines)
    spec = JobSpec(
        job_id=1,
        site={"id": 1, "slug": "fm", "title": "fm"},  # type: ignore[arg-type]
        output_dir=str(out),
        temp_dir=str(tmp),
        config={
            "selection_file": str(selection),
            "rate_per_min": 60,
            "max_consecutive_429": 3,
            "cooldown_min": 30,
            **(config or {}),
        },
        limits={"max_duration_s": max_duration_s},  # type: ignore[arg-type]
    )
    events: list[dict[str, Any]] = []

    class Sink(io.StringIO):
        def write(self, s: str) -> int:
            for line in s.splitlines():
                if line.strip():
                    events.append(json.loads(line))
            return len(s)

    from cairn.engines.protocol import EventWriter

    clock = clock or Clock()
    wall_now = wall if wall is not None else [1_800_000_000.0]
    runner = engine.Runner(
        spec,
        EventWriter(Sink()),
        transport=httpx.MockTransport(fake),
        sleep=clock.sleep,
        clock=clock,
        wall=lambda: wall_now[0],
    )
    return runner, events, out


def result_of(events: list[dict[str, Any]]) -> dict[str, Any]:
    (result,) = [e for e in events if e["type"] == "result"]
    return result


def records(out: Path) -> list[Any]:
    found = []
    for path in sorted((out / "warc").glob("*.warc.gz")):
        with path.open("rb") as fh:
            for record in ArchiveIterator(fh):
                if record.rec_type == "response":
                    found.append(
                        (
                            record.rec_headers.get_header("WARC-Target-URI"),
                            record.rec_headers.get_header("WARC-Date"),
                            record.http_headers,
                            record.raw_stream.read(),
                            record.rec_headers.get_header("WARC-Payload-Digest"),
                        )
                    )
    return found


# ── the engine ───────────────────────────────────────────────────────────


def test_snapshots_become_warc_records_with_their_real_date(tmp_path: Path) -> None:
    fake = FakeWayback()
    page = b"<html>story one</html>"
    fake.snapshots[("20190305102030", STORY.format(1))] = archived(page, gzip_body=True)
    digest = engine.sha1_base32(gzip.compress(page))
    runner, events, out = make_runner(
        tmp_path, fake, [selection_line(STORY.format(1), "20190305102030", "WRONG")]
    )
    assert runner.run() == 0
    assert result_of(events)["status"] == "ok"

    ((uri, date, http_headers, raw, payload_digest),) = records(out)
    assert uri == STORY.format(1)
    assert date == "2019-03-05T10:20:30Z"  # the snapshot time, not today
    assert http_headers.get_header("Server") == "Apache"
    assert http_headers.get_header("Content-Encoding") == "gzip"
    assert http_headers.get_header("Content-Length") == str(len(raw))
    assert raw[:2] == b"\x1f\x8b"  # stored as archived, never decoded
    assert payload_digest == f"sha1:{digest}"

    cdx = (out / "warc" / "part.cdx").read_text().splitlines()
    assert cdx[0] == engine.CDX_HEADER
    assert cdx[1].split(" ")[5] == digest
    ledger = [json.loads(line) for line in (out / engine.LEDGER_FILE).read_text().splitlines()]
    assert ledger[0]["state"] == "done" and ledger[0]["digest_ok"] is False
    kinds = {e["kind"] for e in events if e["type"] == "artifact"}
    assert kinds == {"warc", "cdx", "log"}
    assert (out / RESUME_STATE_FILE).is_file()


def test_an_archived_redirect_is_kept_as_a_redirect(tmp_path: Path) -> None:
    fake = FakeWayback()
    fake.snapshots[("20190101000000", STORY.format(2))] = archived(
        b"", status=302, extra={"Location": "https://web.archive.org/web/20190101000000id_/x"}
    )
    runner, _events, out = make_runner(
        tmp_path, fake, [selection_line(STORY.format(2), "20190101000000")]
    )
    runner.run()
    ((_uri, _date, http_headers, _raw, _d),) = records(out)
    assert http_headers.get_statuscode() == "302"
    assert len(fake.requests) == 1


def test_waybacks_own_redirect_to_a_nearby_snapshot_is_followed(tmp_path: Path) -> None:
    fake = FakeWayback()
    url = STORY.format(3)
    fake.snapshots[("20190101000000", url)] = plain(
        302, headers={"Location": f"/web/20190102000000id_/{url}"}
    )
    fake.snapshots[("20190102000000", url)] = archived(b"<html>three</html>")
    runner, _events, out = make_runner(tmp_path, fake, [selection_line(url, "20190101000000")])
    runner.run()
    assert len(records(out)) == 1
    assert len(fake.requests) == 2


def test_a_missing_or_bad_snapshot_falls_back_to_an_older_one(tmp_path: Path) -> None:
    fake = FakeWayback()
    one, two = STORY.format(4), STORY.format(5)
    # One: the newest copy is gone, the older one is there.
    fake.snapshots[("20170101000000", one)] = archived(b"<html>older four</html>")
    # Two: the newest copy is the site's rate-limit page.
    error_page = b"<html>slow down</html>"
    fake.snapshots[("20240101000000", two)] = archived(error_page)
    fake.snapshots[("20170101000000", two)] = archived(b"<html>older five</html>")
    runner, _events, out = make_runner(
        tmp_path,
        fake,
        [
            selection_line(one, "20240101000000", older=[["20170101000000", "X"]]),
            selection_line(two, "20240101000000", older=[["20170101000000", "Y"]]),
        ],
        config={"bad_digests": [engine.sha1_base32(error_page)]},
    )
    runner.run()
    bodies = {uri: raw for uri, _d, _h, raw, _p in records(out)}
    assert bodies == {one: b"<html>older four</html>", two: b"<html>older five</html>"}


def test_archive_orgs_own_pages_are_never_stored(tmp_path: Path) -> None:
    """A 200 with no Memento-Datetime and no X-Archive-Orig-* is not a snapshot."""
    fake = FakeWayback()
    fake.snapshots[("20190101000000", STORY.format(6))] = plain(
        200, b"<html>Internet Archive: Temporarily Offline</html>"
    )
    runner, events, out = make_runner(
        tmp_path, fake, [selection_line(STORY.format(6), "20190101000000")]
    )
    runner.run()
    assert records(out) == []
    assert result_of(events)["status"] == "failed"


def test_a_second_run_fetches_only_what_is_left(tmp_path: Path) -> None:
    fake = FakeWayback()
    lines = []
    for n in range(4):
        fake.snapshots[("20190101000000", STORY.format(n))] = archived(b"<html>%d</html>" % n)
        lines.append(selection_line(STORY.format(n), "20190101000000"))
    runner, events, out = make_runner(tmp_path, fake, lines)
    # Stop after two, as a pause would.
    original = runner._fetch

    def fetch_then_stop(entry: Any, parts: Any) -> Any:
        result = original(entry, parts)
        if len(fake.requests) == 2:
            runner.stopping = True
        return result

    runner._fetch = fetch_then_stop  # type: ignore[method-assign]
    runner.run()
    first = result_of(events)
    assert first["status"] == "partial" and first["stats"]["resumable"] is True
    assert first["stats"]["remaining"] == 2

    again, events2, _ = make_runner(tmp_path, fake, lines)
    again.run()
    assert len(fake.requests) == 4  # two more, not four
    assert result_of(events2)["stats"]["imported"] == 4
    assert len(records(out)) == 4


def test_a_run_of_429s_pauses_the_import_and_says_when_to_resume(tmp_path: Path) -> None:
    fake = FakeWayback()
    fake.snapshots[("20190101000000", STORY.format(7))] = plain(429)
    runner, events, out = make_runner(
        tmp_path, fake, [selection_line(STORY.format(7), "20190101000000")]
    )
    assert runner.run() == 0
    result = result_of(events)
    assert result["status"] == "partial"
    assert result["stats"]["self_paused"] is True
    assert result["stats"]["resume_after_s"] == 30 * 60
    assert (out / RESUME_STATE_FILE).is_file()


def test_the_daily_budget_pauses_until_it_frees_up(tmp_path: Path) -> None:
    fake = FakeWayback()
    lines = []
    for n in range(5):
        fake.snapshots[("20190101000000", STORY.format(n))] = archived(b"<html>%d</html>" % n)
        lines.append(selection_line(STORY.format(n), "20190101000000"))
    wall = [1_800_000_000.0]
    runner, events, _out = make_runner(tmp_path, fake, lines, config={"daily_budget": 2}, wall=wall)
    runner.run()
    result = result_of(events)
    assert len(fake.requests) == 2
    assert result["stats"]["self_paused"] is True
    assert result["stats"]["resume_after_s"] == 24 * 3600

    # A day later the budget is free again.
    wall[0] += 24 * 3600 + 1
    again, _events2, _ = make_runner(tmp_path, fake, lines, config={"daily_budget": 2}, wall=wall)
    again.run()
    assert len(fake.requests) == 4


def test_the_capture_time_limit_ends_a_segment_not_the_import(tmp_path: Path) -> None:
    fake = FakeWayback()
    lines = []
    for n in range(3):
        fake.snapshots[("20190101000000", STORY.format(n))] = archived(b"<html>%d</html>" % n)
        lines.append(selection_line(STORY.format(n), "20190101000000"))
    clock = Clock()
    runner, events, _ = make_runner(tmp_path, fake, lines, max_duration_s=1, clock=clock)
    # The engine measures elapsed time on the real monotonic clock.
    real = time.monotonic
    ticks = iter([0.0, 0.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0])
    engine_time = engine.time
    engine.time = type("T", (), {"monotonic": staticmethod(lambda: next(ticks, 9.0))})  # type: ignore[assignment]
    try:
        runner.run()
    finally:
        engine.time = engine_time  # type: ignore[assignment]
    assert real is time.monotonic
    result = result_of(events)
    assert result["stats"]["self_paused"] is True
    assert result["stats"]["resume_after_s"] == 60


# ── core ─────────────────────────────────────────────────────────────────


def test_resumed_captures_keep_their_earlier_warcs() -> None:
    earlier = [{"name": "warc/a.warc.gz", "size": 1}, {"name": "warc/part.cdx", "size": 5}]
    now = [{"name": "warc/b.warc.gz", "size": 2}, {"name": "warc/part.cdx", "size": 9}]
    merged = {a["name"]: a["size"] for a in merge_artifacts(earlier, now)}
    assert merged == {"warc/a.warc.gz": 1, "warc/b.warc.gz": 2, "warc/part.cdx": 9}


def make_site(db: Session, settings: Settings, slug: str = "fm") -> Site:
    site = Site(
        folder_id=1,
        slug=slug,
        title=slug,
        seed_url="https://www.fictionmania.tv/",
        primary_host="www.fictionmania.tv",
        archive_path=f"Unfiled/{slug}",
    )
    db.add(site)
    db.flush()
    storage.ensure_site_dirs(settings, site.archive_path)
    return site


def test_an_import_capture_queues_on_archive_org(
    authed: TestClient, db: Session, settings: Settings
) -> None:
    site = make_site(db, settings)
    fresh = Job(
        type="capture",
        site_id=site.id,
        status="queued",
        spec={"kind": "wayback"},
        queued_at=utcnow(),
    )
    resumed = Job(
        type="capture",
        site_id=site.id,
        status="queued",
        spec={"kind": "resume", "resume_capture_id": 1, "request": {"kind": "wayback"}},
        queued_at=utcnow(),
    )
    crawl = Job(
        type="capture", site_id=site.id, status="queued", spec={"kind": "full"}, queued_at=utcnow()
    )
    db.add_all([fresh, resumed, crawl])
    db.flush()
    assert _host_for_job(db, fresh) == "web.archive.org"
    assert _host_for_job(db, resumed) == "web.archive.org"
    assert _host_for_job(db, crawl) == "www.fictionmania.tv"


def test_the_importer_is_not_a_site_engine(
    authed: TestClient, db: Session, settings: Settings
) -> None:
    site = make_site(db, settings)
    db.commit()
    refused = authed.patch(
        f"/api/sites/{site.id}", json={"engine_id": "wayback-import"}, headers=XHR
    )
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "unknown_engine"


def test_an_import_needs_a_survey(authed: TestClient, db: Session, settings: Settings) -> None:
    site = make_site(db, settings)
    db.commit()
    refused = authed.post(f"/api/sites/{site.id}/wayback/import", headers=XHR)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "no_survey"


def test_a_self_paused_capture_is_resumed_when_due(
    authed: TestClient, db: Session, settings: Settings
) -> None:
    from cairn.services.scheduler import Scheduler

    site = make_site(db, settings)
    due = utcnow() - timedelta(minutes=1)
    capture = Capture(
        site_id=site.id,
        kind="wayback",
        engine_id="wayback-import",
        engine_version="1.0.0",
        dir_name="20261101T000000Z-wayback-wayback",
        started_at=utcnow(),
        status="paused",
        request={"kind": "wayback", "resume_at": due.isoformat()},
    )
    later = Capture(
        site_id=site.id,
        kind="wayback",
        engine_id="wayback-import",
        engine_version="1.0.0",
        dir_name="20261101T000001Z-wayback-wayback",
        started_at=utcnow(),
        status="paused",
        request={"kind": "wayback", "resume_at": (utcnow() + timedelta(hours=1)).isoformat()},
    )
    db.add_all([capture, later])
    db.flush()

    class Supervisor:
        def __init__(self) -> None:
            self.specs: list[dict[str, Any]] = []

        def enqueue(self, session: Session, **kwargs: Any) -> Job:
            self.specs.append(kwargs["spec"])
            job = Job(
                type="capture",
                site_id=kwargs["site_id"],
                status="queued",
                spec=kwargs["spec"],
                queued_at=utcnow(),
            )
            session.add(job)
            session.flush()
            return job

    supervisor = Supervisor()
    scheduler = Scheduler.__new__(Scheduler)
    scheduler._supervisor = supervisor  # type: ignore[attr-defined]
    done = scheduler._due_resumes(db, utcnow())
    assert len(done) == 1
    assert supervisor.specs == [
        {"kind": "resume", "resume_capture_id": capture.id, "request": {"kind": "wayback"}}
    ]
    assert "resume_at" not in capture.request
    # Once queued, the site is busy, so the next tick does not queue it again.
    assert scheduler._due_resumes(db, utcnow() + timedelta(hours=2)) == []


# ── end to end: the real engine, a fake archive.org ──────────────────────

PAGES = {
    STORY.format(1): b"<html><head><title>One</title></head><body>UNIQUE-STORY-ONE</body></html>",
    STORY.format(2): b"<html><head><title>Two</title></head><body>UNIQUE-STORY-TWO</body></html>",
}


class _WaybackHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        rest = self.path.split("/web/", 1)[-1]
        _ts, _, url = rest.partition("id_/")
        body = PAGES.get(url)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = gzip.compress(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Memento-Datetime", MEMENTO)
        self.send_header("X-Archive-Orig-Server", "Apache")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def fake_archive() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _WaybackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/web"
    finally:
        server.shutdown()
        server.server_close()


def test_an_import_runs_through_the_queue_and_replays(
    authed: TestClient,
    db: Session,
    settings: Settings,
    fake_archive: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(engine.BASE_ENV, fake_archive)
    site = make_site(db, settings)
    settings_store.put(db, wayback_service.RATE_SETTING, 60)
    selected = [
        wb.Selected(
            key=url.lower(),
            url=url,
            kind="page",
            mimetype="text/html",
            timestamp="20190305102030",
            digest="",
            length=None,
            older=[],
        )
        for url in [*PAGES, STORY.format(404)]
    ]
    wb.write(wayback_service.directory(settings, site), {"version": 1, "rows": 3}, selected)
    db.commit()

    started = authed.post(f"/api/sites/{site.id}/wayback/import", headers=XHR)
    assert started.status_code == 202, started.text
    job_id = started.json()["job_id"]

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        job = authed.get(f"/api/jobs/{job_id}", headers=XHR).json()
        if job["status"] not in ("queued", "running"):
            break
        time.sleep(0.5)
    assert job["status"] == "ok", job

    db.expire_all()
    from sqlalchemy import select

    capture = db.scalars(select(Capture).where(Capture.site_id == site.id)).one()
    assert capture is not None
    assert capture.engine_id == "wayback-import" and capture.kind == "wayback"
    assert capture.status == "partial"  # one URL was not in the archive
    assert capture.url_count == 3 and capture.error_count == 1
    capture_dir = storage.site_dir(settings, site.archive_path) / "captures" / capture.dir_name
    assert (capture_dir / wayback_service.SELECTION_IN_CAPTURE).is_file()

    index = (storage.site_dir(settings, site.archive_path) / "index" / "site.cdxj").read_text()
    assert "readtextstory.html?storyid=1" in index
    assert "20190305102030" in index  # replayed at the snapshot's date

    state = authed.get(f"/api/sites/{site.id}/wayback", headers=XHR).json()
    assert state["import"]["capture_id"] == capture.id


def test_a_copy_archived_uncompressed_still_matches_its_digest(tmp_path: Path) -> None:
    """archive.org gzips such copies on the way out (seen on 2017 snapshots)."""
    fake = FakeWayback()
    page = b"<html>stored plain in 2017</html>"
    fake.snapshots[("20170101000000", STORY.format(8))] = archived(page, gzip_body=True)
    runner, _events, out = make_runner(
        tmp_path,
        fake,
        [selection_line(STORY.format(8), "20170101000000", engine.sha1_base32(page))],
    )
    runner.run()
    (row,) = [json.loads(line) for line in (out / engine.LEDGER_FILE).read_text().splitlines()]
    assert "digest_ok" not in row  # only a mismatch is recorded
