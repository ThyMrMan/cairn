"""A capture has to outlive a database that is briefly unwritable.

Measured on a live instance, three times over. A job finishing its capture
held SQLite's only write lock for the whole post-processor chain — about four
minutes on a 4.7 GB capture — and the capture running beside it hit `database
is locked` on its next batch of URL rows. The exception unwound the task
reading that engine's output. The engine, in a session of its own, kept going
with nobody reading its stdout; wget finished days later; and the job read
`running` until the container restarted. One of them was a nine-day, 157 GB
crawl, of which the database kept 3.7%.

These tests take the lock for real, with a second connection, against a
database file. Connections here wait 50 ms for a lock rather than five
seconds, so contention is quick to provoke — which changes how long things
take, not what they do.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from cairn.config import Settings
from cairn.crypto.sealing import Sealer
from cairn.db import base as db_base
from cairn.db import busy
from cairn.db.base import get_engine, sessionmaker_for
from cairn.db.bootstrap import (
    ensure_directories,
    reconcile_organization,
    run_migrations,
    seed_defaults,
)
from cairn.db.models import Capture, CaptureUrl, Job, PageText, Site
from cairn.db.types import utcnow
from cairn.engines.registry import EngineRegistry
from cairn.services import jobs, orphans, postprocess, storage, textextract
from cairn.services.events import EV_STATUS, EventBus
from cairn.services.jobs import JobSupervisor, RunningJob, _Collector, _Prepared
from cairn.services.sites import create_site
from tests.conftest import TEST_KEY, XHR

# ── a database somebody else can lock ────────────────────────────────────


@contextmanager
def held_lock(path: Path) -> Iterator[None]:
    """SQLite's write lock, taken the way another job's transaction takes it."""
    conn = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield
    finally:
        conn.execute("ROLLBACK")
        conn.close()


def writable(path: Path) -> bool:
    """Whether another connection could take the write lock right now."""
    conn = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def lock_briefly(path: Path, seconds: float) -> None:
    """Hold the lock from another thread for a while, returning once it is held."""
    ready = threading.Event()

    def hold() -> None:
        with held_lock(path):
            ready.set()
            time.sleep(seconds)

    threading.Thread(target=hold, daemon=True).start()
    assert ready.wait(5)


def locked_error() -> OperationalError:
    return OperationalError("UPDATE", {}, sqlite3.OperationalError("database is locked"))


@dataclass
class World:
    settings: Settings
    sessions: sessionmaker[Session]
    supervisor: JobSupervisor
    db_path: Path
    site_id: int
    job_id: int
    capture_id: int
    archive_path: str
    dir_name: str

    def prepared(self, **overrides: Any) -> _Prepared:
        values: dict[str, Any] = {
            "job_id": self.job_id,
            "capture_id": self.capture_id,
            "site_id": self.site_id,
            "archive_path": self.archive_path,
            "dir_name": self.dir_name,
            "command": [],
            "env": {},
            "output_dir": storage.site_dir(self.settings, self.archive_path)
            / storage.CAPTURES_DIR
            / self.dir_name,
            "temp_dir": self.settings.tmp_dir / f"job-{self.job_id}",
            "max_pages": None,
            "seeds": ["https://blog.example/"],
        }
        values.update(overrides)
        return _Prepared(**values)

    def collector(self, **overrides: Any) -> _Collector:
        return _Collector(self.supervisor, self.prepared(**overrides), self.supervisor._bus)

    def url_rows(self) -> int:
        with self.sessions() as s:
            return s.scalar(select(func.count(CaptureUrl.id))) or 0

    def rows(self) -> tuple[Job, Capture, Site]:
        with self.sessions() as s:
            job = s.get(Job, self.job_id)
            capture = s.get(Capture, self.capture_id)
            site = s.get(Site, self.site_id)
            assert job is not None and capture is not None and site is not None
            return job, capture, site

    def strand(
        self, *, minutes_ago: float = 10, pid: int | None = None, capture: str = "running"
    ) -> None:
        """Make the job one nothing in this process is supervising."""
        with self.sessions() as s:
            job = s.get(Job, self.job_id)
            assert job is not None
            job.started_at = utcnow() - timedelta(minutes=minutes_ago)
            job.pid = pid
            row = s.get(Capture, self.capture_id)
            assert row is not None
            row.status = capture
            s.commit()


@pytest.fixture
def world(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """A migrated database file, a supervisor over it that is not running, and
    one site with a capture in progress."""
    monkeypatch.setattr(db_base, "BUSY_TIMEOUT_MS", 50)
    ensure_directories(settings)
    run_migrations(settings)
    engine = get_engine(settings.db_url)
    sessions = sessionmaker_for(engine)
    registry = EngineRegistry(settings)

    with sessions() as s:
        seed_defaults(s, settings)
        registry.refresh(s)
        s.commit()
        reconcile_organization(s, settings)
        s.commit()
        site = create_site(s, settings, seed_url="https://blog.example/", title="Blog")
        site.status = "capturing"
        job = Job(type="capture", site_id=site.id, status="running", spec={}, attempts=1)
        job.started_at = utcnow()
        s.add(job)
        s.flush()
        dir_name = storage.capture_dir_name(utcnow(), "feed", site.engine_id)
        capture = Capture(
            site_id=site.id,
            job_id=job.id,
            kind="feed",
            engine_id=site.engine_id,
            dir_name=dir_name,
            started_at=utcnow(),
            status="running",
        )
        s.add(capture)
        s.commit()
        ids = (site.id, job.id, capture.id, site.archive_path)

    storage.ensure_capture_dirs(settings, ids[3], dir_name)
    supervisor = JobSupervisor(settings, sessions, EventBus(), registry, Sealer(TEST_KEY.encode()))
    yield World(
        settings=settings,
        sessions=sessions,
        supervisor=supervisor,
        db_path=settings.db_path,
        site_id=ids[0],
        job_id=ids[1],
        capture_id=ids[2],
        archive_path=ids[3],
        dir_name=dir_name,
    )
    engine.dispose()


def url_event(n: int) -> str:
    return json.dumps(
        {"type": "url", "url": f"https://blog.example/p/{n}", "status": 200, "mime": "text/html"}
    )


# ── the rows a capture records while another job holds the lock ──────────


async def test_a_capture_keeps_its_rows_while_another_job_holds_the_lock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure itself. `handle` used to raise straight out of here, and
    the batch it raised on had already been swapped out of `_pending`."""
    monkeypatch.setattr(jobs, "URL_RETRY_FIRST_SECONDS", 0.05)
    collector = world.collector()

    with held_lock(world.db_path):
        for n in range(1200):
            await collector.handle(url_event(n))
        assert len(collector._pending) == 1200
        assert collector._refusals >= 1
        assert world.url_rows() == 0

    await collector.drain(budget_s=10)
    assert world.url_rows() == 1200
    assert collector.unrecorded == 0
    assert collector.report() == []


async def test_rows_land_in_the_order_they_were_fetched(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "URL_RETRY_FIRST_SECONDS", 0.05)
    monkeypatch.setattr(jobs, "URL_BATCH_SIZE", 4)
    collector = world.collector()
    with held_lock(world.db_path):
        for n in range(10):
            await collector.handle(url_event(n))
    await collector.drain(budget_s=10)
    with world.sessions() as s:
        urls = s.scalars(select(CaptureUrl.url).order_by(CaptureUrl.id)).all()
    assert urls == [f"https://blog.example/p/{n}" for n in range(10)]


async def test_a_database_that_stays_locked_costs_rows_not_the_capture(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And says so. The per-URL table is short; the WARCs are not, and the
    report has to make that distinction for whoever reads it later."""
    monkeypatch.setattr(jobs, "URL_RETRY_FIRST_SECONDS", 0.05)
    collector = world.collector()
    with held_lock(world.db_path):
        for n in range(30):
            await collector.handle(url_event(n))
        await collector.drain(budget_s=0.3)

    assert collector.unrecorded == 30
    assert collector._pending == []
    [note] = collector.report()
    assert "30 fetched URL(s)" in note
    assert "WARCs" in note


async def test_the_backlog_is_bounded(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """A database unwritable for hours must not become a memory problem."""
    monkeypatch.setattr(jobs, "MAX_PENDING_URLS", 10)
    monkeypatch.setattr(jobs, "URL_BATCH_SIZE", 5)
    monkeypatch.setattr(jobs, "URL_RETRY_FIRST_SECONDS", 60.0)
    collector = world.collector()
    with held_lock(world.db_path):
        for n in range(25):
            await collector.handle(url_event(n))

    assert len(collector._pending) == 10
    assert collector.unrecorded == 15
    # The oldest go: the newest rows are the ones still worth having.
    assert collector._pending[-1]["url"].endswith("/p/24")


async def test_rows_the_database_rejects_are_dropped_and_the_rest_go_in(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not every refusal is contention. A row the database objects to will be
    objected to forever, and retrying it would stall every row behind it."""
    monkeypatch.setattr(jobs, "URL_BATCH_SIZE", 3)
    collector = world.collector()

    def row(url: str | None) -> dict[str, Any]:
        return {"capture_id": world.capture_id, "url": url, "host": "blog.example"}

    collector._pending = [
        row("https://blog.example/a"),
        row("https://blog.example/b"),
        row("https://blog.example/c"),
        row(None),
        row(None),
        row(None),
        row("https://blog.example/d"),
    ]
    await collector.flush()
    assert collector.unrecorded == 3
    assert collector._write_after == 0.0
    assert world.url_rows() == 3

    await collector.flush()
    assert world.url_rows() == 4
    assert collector._pending == []


async def test_a_refused_progress_write_does_not_end_the_capture(world: World) -> None:
    collector = world.collector()
    with held_lock(world.db_path):
        await collector.handle(json.dumps({"type": "progress", "done": 5, "bytes": 10}))
    # And the URL writes wait along with it rather than walking into the lock.
    assert collector._write_after > time.monotonic() - 1


async def test_one_bad_event_does_not_end_the_capture(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    collector = world.collector()

    def broken(_event: Any) -> None:
        raise RuntimeError("an artifact this code could not handle")

    monkeypatch.setattr(collector, "_record_artifact", broken)
    await collector.take(json.dumps({"type": "artifact", "kind": "warc", "path": "warc/x"}))
    await collector.take(url_event(1))
    assert collector.malformed == 1
    assert collector.url_count == 1


# ── post-processing, and the lock it used to hold ────────────────────────


def probe_step(order: int, into: list[bool], path: Path) -> postprocess.Step:
    def probe(_ctx: postprocess.Context) -> None:
        into.append(writable(path))

    return postprocess.Step(f"probe-{order}", order, False, probe)


def extracted_pages(count: int) -> list[textextract.Page]:
    return [
        textextract.Page(
            url=f"https://blog.example/p/{n}",
            title=f"Post {n}",
            blocks=[f"words of post {n}"],
            timestamp="20260913013409",
            offset=n,
            length=1,
        )
        for n in range(count)
    ]


def test_post_processing_runs_with_the_database_writable(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole chain, with a probe before it and after it. The first catches
    the flush `_finalize` used to do before the chain; the second catches any
    step that wrote through the chain's session, which holds the lock through
    every step after it. Text extraction is given pages so that the one step
    that does write actually has something to write."""
    monkeypatch.setattr(
        textextract,
        "extract_capture",
        lambda *_a, **_k: SimpleNamespace(pages=extracted_pages(3), dropped_blocks=0),
    )
    seen: list[bool] = []
    monkeypatch.setattr(
        postprocess,
        "CHAIN",
        [
            probe_step(1, seen, world.db_path),
            *postprocess.CHAIN,
            probe_step(99, seen, world.db_path),
        ],
    )
    world.supervisor._finalize(world.prepared(), world.collector(), "ok", 0, "", False)
    assert seen == [True, True]
    job, capture, _site = world.rows()
    assert job.status == "ok"
    assert capture.status == "ok"
    with world.sessions() as s:
        assert s.scalar(select(func.count(PageText.id))) == 3


def test_the_crawl_is_on_record_before_post_processing_starts(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """So a restart during a slow chain costs the post-processing, not the
    record of a crawl that finished — which is what lost 157 GB its counts."""
    seen: dict[str, Any] = {}

    def look(_ctx: postprocess.Context) -> None:
        job, capture, _site = world.rows()
        seen.update(
            job=job.status,
            phase=(job.progress or {}).get("phase"),
            capture=capture.status,
            urls=capture.url_count,
        )

    monkeypatch.setattr(postprocess, "CHAIN", [postprocess.Step("look", 1, False, look)])
    collector = world.collector()
    collector.url_count = 42
    world.supervisor._finalize(world.prepared(), collector, "ok", 0, "", False)
    assert seen == {"job": "running", "phase": "post-processing", "capture": "ok", "urls": 42}


def test_what_post_processing_decides_is_what_is_recorded(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its changes are carried out of the read-only session, not lost with it."""

    def judge(ctx: postprocess.Context) -> None:
        ctx.capture.status = "partial"
        ctx.capture.url_count = 7
        ctx.site.size_bytes = 12_345

    monkeypatch.setattr(postprocess, "CHAIN", [postprocess.Step("judge", 1, False, judge)])
    collector = world.collector()
    collector.url_count = 9
    world.supervisor._finalize(world.prepared(), collector, "ok", 0, "", False)

    job, capture, site = world.rows()
    assert capture.status == "partial"
    assert capture.url_count == 7
    assert site.size_bytes == 12_345
    assert job.status == "ok"
    assert job.pid is None
    assert job.finished_at is not None
    assert job.progress == {"done": 9, "bytes": 0, "status": "ok"}
    assert site.status == "ready"
    assert site.last_capture_at is not None


def test_a_field_no_step_used_to_set_is_not_lost(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The results are carried out of a session that is then rolled back, so
    a step that sets something new must not have its work vanish with it."""

    def annotate(ctx: postprocess.Context) -> None:
        ctx.capture.engine_version = "9.9-post"
        ctx.site.notes = "checked by a later step"

    monkeypatch.setattr(postprocess, "CHAIN", [postprocess.Step("annotate", 1, False, annotate)])
    world.supervisor._finalize(world.prepared(), world.collector(), "ok", 0, "", False)
    _job, capture, site = world.rows()
    assert capture.engine_version == "9.9-post"
    assert site.notes == "checked by a later step"


def test_closing_the_job_waits_out_a_busy_database(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another writer takes the lock as the chain ends and keeps it for longer
    than a connection waits on its own. The job still closes."""
    monkeypatch.setattr(busy, "FIRST_DELAY_S", 0.05)

    def contend(_ctx: postprocess.Context) -> None:
        lock_briefly(world.db_path, 0.4)

    monkeypatch.setattr(postprocess, "CHAIN", [postprocess.Step("contend", 99, False, contend)])
    world.supervisor._finalize(world.prepared(), world.collector(), "ok", 0, "", False)
    job, _capture, _site = world.rows()
    assert job.status == "ok"


def test_text_is_indexed_in_short_transactions_of_its_own(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thousands of pages used to be indexed through the chain's session, in
    one transaction that then stayed open for every step after it."""
    monkeypatch.setattr(
        textextract,
        "extract_capture",
        lambda *_a, **_k: SimpleNamespace(pages=extracted_pages(5), dropped_blocks=0),
    )
    monkeypatch.setattr(postprocess, "TEXT_INDEX_BATCH", 2)

    transactions: list[str] = []

    def write(apply: Callable[[Session], Any], *, what: str) -> Any:
        transactions.append(what)
        return world.supervisor._write(apply, what=what)

    with world.sessions() as s:
        capture = s.get(Capture, world.capture_id)
        site = s.get(Site, world.site_id)
        assert capture is not None and site is not None
        ctx = postprocess.Context(
            session=s,
            settings=world.settings,
            capture=capture,
            site=site,
            output_dir=world.prepared().output_dir,
            tool_version=None,
            stats={},
            scope={},
            seeds=[],
            seed_source={},
            artifacts=[],
            warnings=[],
            write=write,
        )
        postprocess.step_text_extract(ctx)
        # The chain's own session is still holding nothing.
        assert writable(world.db_path)

    assert len(transactions) == 3
    assert ctx.stats["text_pages"] == 5
    with world.sessions() as s:
        assert s.scalar(select(func.count(PageText.id))) == 5


# ── an engine is never left running unwatched ────────────────────────────

ENGINE_FOREVER = """
import json, time
n = 0
while True:
    n += 1
    print(json.dumps({"type": "url", "url": f"https://blog.example/p/{n}"}), flush=True)
    time.sleep(0.01)
"""

ENGINE_BRIEF = """
import json
print(json.dumps({"type": "started", "tool_version": "test"}), flush=True)
for n in range(3):
    print(json.dumps({"type": "url", "url": f"https://blog.example/p/{n}", "status": 200}),
          flush=True)
print(json.dumps({"type": "result", "status": "ok", "stats": {}}), flush=True)
"""


async def wait_for(condition: Callable[[], bool], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.05)


async def test_a_supervisor_that_stops_watching_stops_its_engine(world: World) -> None:
    """Cancellation is the realistic way out of the read loop now that no
    database error leads there. The engine used to be left running — in its
    own session, so nothing else would stop it either."""
    prepared = world.prepared(command=[sys.executable, "-c", ENGINE_FOREVER])
    prepared.temp_dir.mkdir(parents=True, exist_ok=True)
    running = RunningJob(job_id=world.job_id)
    task = asyncio.create_task(world.supervisor._execute(running, prepared))
    try:
        await wait_for(lambda: running.process is not None)
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert running.process is not None
        assert running.process.returncode is not None
    finally:
        if running.process is not None and running.process.returncode is None:
            running.process.kill()
            await running.process.wait()


async def test_failing_to_record_the_pid_does_not_abandon_the_crawl(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write that used to fail *before* the read loop started, which
    orphaned the engine with no pid on record for anything to find it by."""

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise locked_error()

    monkeypatch.setattr(world.supervisor, "_record_pid", refuse)
    monkeypatch.setattr(postprocess, "CHAIN", [])
    prepared = world.prepared(command=[sys.executable, "-c", ENGINE_BRIEF])
    prepared.temp_dir.mkdir(parents=True, exist_ok=True)

    await world.supervisor._execute(RunningJob(job_id=world.job_id), prepared)

    job, capture, _site = world.rows()
    assert job.status == "ok"
    assert capture.status == "ok"
    assert world.url_rows() == 3


# Raw writes on purpose. A Python engine blocked inside a *buffered* write
# does not reach a handler that writes to the same stream: CPython runs the
# handler inside that write, the handler's write re-enters the same buffer,
# and the process dies of "reentrant call" with exit status 1 — which is how
# this test's first version failed on Linux, where it is the only test here
# that runs at all.
ENGINE_STUCK = """
import os, signal, time

def on_term(signum, frame):
    # An engine on its way out still has things to say; this is more than a
    # pipe holds, so it finishes only if somebody reads.
    os.write(1, b"x" * 200_000)
    os._exit(0)

signal.signal(signal.SIGTERM, on_term)
os.write(1, b"y" * 1_000_000)
time.sleep(60)
"""


@pytest.mark.skipif(os.name == "nt", reason="needs POSIX signal handlers and process groups")
async def test_an_engine_stuck_on_a_full_pipe_is_still_stopped() -> None:
    """The state the stranded engines were found in — blocked writing to a
    pipe nobody reads — and an engine whose way out writes more than the pipe
    holds, as the wget engine's does when it reports every revisit. Without
    the output drained it sits there until it is killed."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        ENGINE_STUCK,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        await asyncio.sleep(0.5)
        await jobs._halt(proc, grace_s=10)
        # Its own handler ran to the end: stopped, not killed.
        assert proc.returncode == 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


def test_the_wget_engine_stops_wget_before_it_says_so() -> None:
    """The handler's log line is a write to stdout. With nobody reading, that
    write never completes — it blocks, or raises if the signal landed inside
    another write to the same stream — and wget, asked second, was never
    asked."""
    from cairn.engines.wget import Runner

    runner = Runner.__new__(Runner)
    runner.terminating = False
    terminated: list[bool] = []
    runner.proc = SimpleNamespace(  # type: ignore[assignment]
        poll=lambda: None, terminate=lambda: terminated.append(True)
    )

    def blocked_write(*_args: Any, **_kwargs: Any) -> None:
        raise BrokenPipeError("stdout is not being read")

    runner.events = SimpleNamespace(log=blocked_write)  # type: ignore[assignment]
    with pytest.raises(BrokenPipeError):
        runner.request_stop(15, None)
    assert terminated == [True]


# ── writes that record how a job ended ───────────────────────────────────


def test_a_write_waits_out_a_lock_that_clears(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(busy, "FIRST_DELAY_S", 0.05)
    lock_briefly(world.db_path, 0.4)
    attempts: list[int] = []

    def apply(session: Session) -> None:
        attempts.append(1)
        job = session.get(Job, world.job_id)
        assert job is not None
        job.error = "noted"

    busy.write(world.sessions, apply, what="test")
    assert len(attempts) >= 2
    assert world.rows()[0].error == "noted"


def test_a_write_gives_up_after_its_budget(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(busy, "FIRST_DELAY_S", 0.05)

    def apply(session: Session) -> None:
        job = session.get(Job, world.job_id)
        assert job is not None
        job.error = "never"

    started = time.monotonic()
    with held_lock(world.db_path), pytest.raises(OperationalError):
        busy.write(world.sessions, apply, what="test", budget_s=0.3)
    assert time.monotonic() - started < 2


def test_only_contention_is_retried(world: World) -> None:
    """Retrying a constraint violation would turn an error into a hang."""
    attempts: list[int] = []

    def apply(session: Session) -> None:
        attempts.append(1)
        session.add(CaptureUrl(capture_id=999_999, url="x", host="x", is_revisit=False))

    with pytest.raises(IntegrityError):
        busy.write(world.sessions, apply, what="test", budget_s=2)
    assert attempts == [1]


def test_a_retry_rebuilds_the_write_rather_than_replaying_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A counter must not be bumped twice. `_settle_feed_items` increments
    `capture_failures`, and the retry is only safe because each attempt starts
    from what the database holds."""
    monkeypatch.setattr(busy, "FIRST_DELAY_S", 0.01)
    seen: list[int] = []

    def apply(session: Session) -> None:
        job = session.get(Job, world.job_id)
        assert job is not None
        job.attempts += 1
        seen.append(job.attempts)
        if len(seen) == 1:
            raise locked_error()

    busy.write(world.sessions, apply, what="test")
    assert seen == [2, 2]
    assert world.rows()[0].attempts == 2


def test_contention_is_told_apart_from_everything_else(world: World) -> None:
    with held_lock(world.db_path):
        conn = sqlite3.connect(world.db_path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError) as raw:
                conn.execute("UPDATE jobs SET error = 'x'")
        finally:
            conn.close()
    assert busy.is_busy(raw.value)
    assert busy.is_busy(OperationalError("UPDATE", {}, raw.value))
    assert busy.is_busy(locked_error())
    assert busy.is_transient(raw.value)

    conn = sqlite3.connect(world.db_path)
    try:
        with pytest.raises(sqlite3.OperationalError) as missing:
            conn.execute("SELECT * FROM no_such_table")
    finally:
        conn.close()
    assert not busy.is_busy(missing.value)
    # Not contention, but still the database refusing rather than the data
    # being wrong — a caller holding rows in memory may try again later.
    assert busy.is_transient(missing.value)

    violation = IntegrityError("INSERT", {}, sqlite3.IntegrityError("FOREIGN KEY constraint"))
    assert not busy.is_busy(violation)
    assert not busy.is_transient(violation)


# ── jobs nothing is supervising ──────────────────────────────────────────


def test_a_running_job_nothing_supervises_is_closed(world: World) -> None:
    world.strand()
    assert world.supervisor._settle_strays(set()) == [world.job_id]
    job, capture, site = world.rows()
    assert job.status == "interrupted"
    assert job.finished_at is not None
    assert "lost track" in (job.error or "")
    assert capture.status == "interrupted"
    assert site.status == "ready"


def test_a_job_this_process_is_running_is_left_alone(world: World) -> None:
    world.strand()
    assert world.supervisor._settle_strays({world.job_id}) == []
    assert world.rows()[0].status == "running"


def test_a_job_claimed_moments_ago_is_left_alone(world: World) -> None:
    world.strand(minutes_ago=0)
    assert world.supervisor._settle_strays(set()) == []
    assert world.rows()[0].status == "running"


def test_a_stray_whose_crawl_had_finished_says_so(world: World) -> None:
    """The case the live instance produced: wget done, WARCs whole, and a job
    whose only unfinished business was post-processing."""
    world.strand(capture="ok")
    world.supervisor._settle_strays(set())
    job, capture, _site = world.rows()
    assert job.status == "interrupted"
    assert "post-processed" in (job.error or "")
    assert "WARCs are complete" in (job.error or "")
    assert capture.status == "ok"


def test_a_stray_engine_is_stopped_by_its_pid(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.strand(pid=4242)
    reaped: list[tuple[int, str]] = []

    def reap(pid: int, marker: str, **_kwargs: Any) -> str:
        reaped.append((pid, marker))
        return orphans.STOPPED

    monkeypatch.setattr(orphans, "reap", reap)
    world.supervisor._settle_strays(set())
    assert reaped == [(4242, str(world.settings.tmp_dir / f"job-{world.job_id}"))]
    assert world.rows()[0].pid is None


def test_a_pid_the_reaper_could_not_judge_is_kept(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """So Cancel can try again, as after a restart."""
    world.strand(pid=4242)
    monkeypatch.setattr(orphans, "reap", lambda *_a, **_k: orphans.UNSUPPORTED)
    world.supervisor._settle_strays(set())
    job, _capture, _site = world.rows()
    assert job.status == "interrupted"
    assert job.pid == 4242


def test_the_site_stays_capturing_while_another_job_works_on_it(world: World) -> None:
    world.strand()
    with world.sessions() as s:
        other = Job(type="capture", site_id=world.site_id, status="running", spec={}, attempts=1)
        other.started_at = utcnow()
        s.add(other)
        s.commit()
        other_id = other.id
    world.supervisor._settle_strays({other_id})
    assert world.rows()[2].status == "capturing"


async def test_the_check_announces_what_it_closed(world: World) -> None:
    world.strand()
    assert await world.supervisor._reconcile_strays() == [world.job_id]
    events = world.supervisor._bus.history(world.job_id)
    assert [e.data.get("status") for e in events if e.event == EV_STATUS] == ["interrupted"]


async def test_cancel_reaches_a_running_job_nothing_supervises(world: World) -> None:
    """Before, a job reading `running` was taken for one being claimed, the
    request was parked for a task that would never exist, and the click did
    nothing — the complaint about the first stranded crawl, exactly."""
    world.strand()
    assert await world.supervisor.cancel(world.job_id)
    job, capture, site = world.rows()
    assert job.status == "cancelled"
    assert capture.status == "cancelled"
    assert site.status == "ready"
    assert world.job_id not in world.supervisor._cancel_requests


async def test_a_fresh_claim_still_holds_the_request(world: World) -> None:
    world.strand(minutes_ago=0)
    assert await world.supervisor.cancel(world.job_id)
    assert world.job_id in world.supervisor._cancel_requests
    assert world.rows()[0].status == "running"


def test_boot_says_when_only_post_processing_was_lost(world: World) -> None:
    world.strand(capture="partial")
    world.supervisor._recover_interrupted()
    job, capture, _site = world.rows()
    assert job.status == "interrupted"
    assert "post-processed" in (job.error or "")
    assert capture.status == "partial"


def test_boot_still_calls_an_interrupted_crawl_what_it_was(world: World) -> None:
    world.strand()
    world.supervisor._recover_interrupted()
    job, capture, _site = world.rows()
    assert job.error == "the container stopped while this job was running"
    assert capture.status == "interrupted"


# ── what the API shows while post-processing ─────────────────────────────


def test_a_capture_being_post_processed_cannot_be_deleted(authed: TestClient) -> None:
    """Its status already carries the engine's verdict, so it no longer reads
    `running` — but its job is still reading the files."""
    factory = authed.app.state.sessionmaker  # type: ignore[attr-defined]
    settings: Settings = authed.app.state.settings  # type: ignore[attr-defined]
    created = authed.post("/api/sites", json={"seed_url": "https://blog.example/"}, headers=XHR)
    assert created.status_code == 201, created.text
    with factory() as s:
        site = s.get(Site, created.json()["id"])
        job = Job(type="capture", site_id=site.id, status="running", spec={}, attempts=1)
        s.add(job)
        s.flush()
        dir_name = storage.capture_dir_name(utcnow(), "full", site.engine_id)
        capture = Capture(
            site_id=site.id,
            job_id=job.id,
            kind="full",
            engine_id=site.engine_id,
            dir_name=dir_name,
            started_at=utcnow(),
            status="ok",
        )
        s.add(capture)
        s.commit()
        storage.ensure_capture_dirs(settings, site.archive_path, dir_name)
        capture_id = capture.id

    res = authed.delete(f"/api/captures/{capture_id}?force=true", headers=XHR)
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "capture_running"


def test_pause_is_not_offered_once_the_crawl_is_over(world: World) -> None:
    from cairn.api.routers.jobs import _summary

    registry = SimpleNamespace(
        get=lambda _engine_id: SimpleNamespace(capabilities={"resumable": True})
    )
    with world.sessions() as s:
        job = s.get(Job, world.job_id)
        assert job is not None
        job.progress = {"done": 10}
        assert _summary(s, job, registry).can_pause
        job.progress = {"done": 10, "phase": jobs.PHASE_POSTPROCESSING}
        assert not _summary(s, job, registry).can_pause
