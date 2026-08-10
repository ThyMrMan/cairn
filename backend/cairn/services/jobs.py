"""The job supervisor: queue, spawn, translate, finalize (docs/02, docs/05).

One asyncio task per running job, each owning an engine subprocess and turning
its NDJSON into database rows and bus events. Everything here has to survive
the container being restarted mid-crawl, because on Unraid it will be.

Design points worth keeping:

**Engines run out-of-process.** A crash, a memory blowup, or a hung socket in
a six-hour crawl takes down that process and nothing else. It also means an
addon engine needs no Python and gets no access to the database.

**A job that was running when the process died is `interrupted`, not
`running`.** Boot reconciles that, because a row claiming to be running with
no process behind it blocks the queue forever and shows the user a progress
bar that will never move.

**Database work happens off the event loop.** SQLAlchemy's session is sync; a
batch of a thousand URL rows inside the loop would stall every other job's
output and every SSE heartbeat.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from cairn.config import Settings
from cairn.crypto.sealing import Sealer
from cairn.db.models import Capture, Job, Site
from cairn.db.types import utcnow
from cairn.engines.protocol import (
    JOB_SPEC_FILE,
    PROTOCOL_VERSION,
    SEED_FILE,
    ArtifactEvent,
    LogEvent,
    ProgressEvent,
    ResultEvent,
    StartedEvent,
    UrlEvent,
    WarningEvent,
    parse_event,
)
from cairn.engines.registry import EngineRegistry
from cairn.logging import get_logger
from cairn.services import postprocess, profiles, sites, storage
from cairn.services.events import (
    EV_ARTIFACT,
    EV_LOG,
    EV_PROGRESS,
    EV_STATUS,
    EV_URL,
    EV_WARNING,
    EventBus,
)
from cairn.services.storage import StoragePathError

log = get_logger(__name__)

URL_BATCH_SIZE = 500
URL_BATCH_SECONDS = 2.0
PROGRESS_WRITE_SECONDS = 2.0
DISPATCH_POLL_SECONDS = 1.0
TERM_GRACE_SECONDS = 60
STDERR_LIMIT = 64 * 1024
# stdout lines from an engine are bounded so a runaway engine printing one
# enormous line cannot exhaust memory.
LINE_LIMIT = 1024 * 1024


class JobError(RuntimeError):
    """A job could not be prepared or started."""


@dataclass(slots=True)
class RunningJob:
    job_id: int
    process: asyncio.subprocess.Process | None = None
    cancelled: bool = False
    capture_id: int | None = None
    task: asyncio.Task[None] | None = None
    started: float = field(default_factory=time.monotonic)


class JobSupervisor:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        bus: EventBus,
        registry: EngineRegistry,
        sealer: Sealer,
    ) -> None:
        self._settings = settings
        self._sessions = session_factory
        self._bus = bus
        self._registry = registry
        self._sealer = sealer
        self._running: dict[int, RunningJob] = {}
        self._wake = asyncio.Event()
        self._dispatcher: asyncio.Task[None] | None = None
        self._stopping = False

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        await asyncio.to_thread(self._recover_interrupted)
        self._dispatcher = asyncio.create_task(self._dispatch_loop(), name="cairn-dispatch")

    async def stop(self) -> None:
        """Ask running jobs to stop, then wait briefly for them to flush.

        SIGTERM rather than SIGKILL: the engine's contract is that it closes
        and flushes its WARC on SIGTERM, so a restart costs a partial capture
        instead of a truncated archive.
        """
        self._stopping = True
        self._wake.set()
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dispatcher

        for running in list(self._running.values()):
            self._signal(running, signal.SIGTERM)
        tasks = [r.task for r in self._running.values() if r.task is not None]
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=TERM_GRACE_SECONDS)
            for task in pending:
                task.cancel()

    def _recover_interrupted(self) -> None:
        with self._sessions() as session:
            stale = session.scalars(select(Job).where(Job.status == "running")).all()
            for job in stale:
                job.status = "interrupted"
                job.finished_at = utcnow()
                job.error = "the container stopped while this job was running"
                job.pid = None
            captures = session.scalars(select(Capture).where(Capture.status == "running")).all()
            for capture in captures:
                capture.status = "interrupted"
                capture.finished_at = utcnow()
            session.execute(update(Site).where(Site.status == "capturing").values(status="ready"))
            session.commit()
            if stale or captures:
                log.warning(
                    "recovered interrupted work",
                    extra={"jobs": len(stale), "captures": len(captures)},
                )

    # ── queueing ─────────────────────────────────────────────────────────

    def enqueue(
        self,
        session: Session,
        *,
        job_type: str,
        site_id: int | None,
        spec: dict[str, Any],
        priority: int = 100,
    ) -> Job:
        job = Job(
            type=job_type,
            site_id=site_id,
            status="queued",
            priority=priority,
            spec=spec,
            queued_at=utcnow(),
        )
        session.add(job)
        session.flush()
        return job

    def notify(self) -> None:
        """Wake the dispatcher — called after a job is committed."""
        # No running loop when enqueued from a CLI command; the next poll
        # picks the job up regardless, so this is an optimisation, not a
        # requirement.
        with contextlib.suppress(RuntimeError):
            self._wake.set()

    async def cancel(self, job_id: int) -> bool:
        running = self._running.get(job_id)
        if running is None:
            # Queued but not started: mark it cancelled so it never runs.
            return await asyncio.to_thread(self._cancel_queued, job_id)
        running.cancelled = True
        self._signal(running, signal.SIGTERM)
        return True

    def _cancel_queued(self, job_id: int) -> bool:
        with self._sessions() as session:
            job = session.get(Job, job_id)
            if job is None or job.status != "queued":
                return False
            job.status = "cancelled"
            job.finished_at = utcnow()
            session.commit()
            return True

    def _signal(self, running: RunningJob, sig: int) -> None:
        proc = running.process
        if proc is None or proc.returncode is not None:
            return
        # The process may exit between the returncode check and the signal;
        # that is the outcome we wanted anyway.
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.send_signal(sig)

    # ── dispatch ─────────────────────────────────────────────────────────

    async def _dispatch_loop(self) -> None:
        while not self._stopping:
            try:
                await self._dispatch_once()
            except Exception:
                log.exception("dispatcher iteration failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=DISPATCH_POLL_SECONDS)
            self._wake.clear()

    async def _dispatch_once(self) -> None:
        capacity = self._settings.max_concurrent_jobs - len(self._running)
        if capacity <= 0:
            return
        claimed = await asyncio.to_thread(self._claim, capacity)
        for job_id in claimed:
            running = RunningJob(job_id=job_id)
            self._running[job_id] = running
            running.task = asyncio.create_task(self._run(running), name=f"cairn-job-{job_id}")

    def _claim(self, capacity: int) -> list[int]:
        """Move up to `capacity` queued jobs to running, in priority order."""
        with self._sessions() as session:
            rows = session.scalars(
                select(Job)
                .where(Job.status == "queued")
                .order_by(Job.priority.asc(), Job.queued_at.asc())
                .limit(capacity)
            ).all()
            claimed = []
            for job in rows:
                job.status = "running"
                job.started_at = utcnow()
                job.attempts += 1
                claimed.append(job.id)
            session.commit()
            return claimed

    # ── execution ────────────────────────────────────────────────────────

    async def _run(self, running: RunningJob) -> None:
        job_id = running.job_id
        temp_dir = self._settings.tmp_dir / f"job-{job_id}"
        try:
            job_type = await asyncio.to_thread(self._job_type, job_id)
            if job_type == "discovery":
                # Discovery writes no WARC and needs database access to record
                # the hosts it found, so it runs in-process rather than as an
                # engine subprocess. It still goes through the queue, the event
                # bus and crash recovery like everything else.
                await self._run_discovery(running, temp_dir)
                return
            prepared = await asyncio.to_thread(self._prepare, job_id, temp_dir)
            running.capture_id = prepared.capture_id
            await self._execute(running, prepared)
        except asyncio.CancelledError:
            await asyncio.to_thread(self._fail, job_id, running.capture_id, "cancelled")
            raise
        except Exception as exc:
            log.exception("job failed", extra={"job": job_id})
            await asyncio.to_thread(
                self._fail, job_id, running.capture_id, f"{type(exc).__name__}: {exc}"
            )
        finally:
            self._running.pop(job_id, None)
            # Plaintext cookie jars live here; remove them the moment the job
            # ends rather than waiting for the next boot sweep (docs/06).
            await asyncio.to_thread(_remove_tree, temp_dir)
            self._wake.set()

    def _job_type(self, job_id: int) -> str:
        with self._sessions() as session:
            job = session.get(Job, job_id)
            return job.type if job else ""

    # ── discovery ────────────────────────────────────────────────────────

    async def _run_discovery(self, running: RunningJob, temp_dir: Path) -> None:
        from cairn.discovery.runner import DiscoveryOptions, discover

        job_id = running.job_id
        setup = await asyncio.to_thread(self._prepare_discovery, job_id, temp_dir)
        self._bus.publish(job_id, EV_STATUS, {"status": "running"})

        def progress(stage: str, fields: dict[str, Any]) -> None:
            message = fields.pop("message", None)
            detail = ", ".join(f"{k}={v}" for k, v in fields.items())
            self._bus.publish(
                job_id,
                EV_LOG,
                {"level": "info", "msg": f"{stage}: {message or detail or '…'}"},
            )
            if fields:
                self._bus.publish(job_id, EV_PROGRESS, {"done": fields.get("pages", 0), **fields})

        result = await discover(setup["seed_url"], DiscoveryOptions(**setup["options"]), progress)

        status = "failed" if result.errors and not result.hosts else "ok"
        await asyncio.to_thread(self._finish_discovery, job_id, result, status)
        self._bus.publish(
            job_id,
            EV_STATUS,
            {
                "status": status,
                "hosts": len(result.hosts),
                "urls": len(result.all_urls),
                "error": result.errors[0] if (status == "failed" and result.errors) else None,
            },
        )

    def _prepare_discovery(self, job_id: int, temp_dir: Path) -> dict[str, Any]:
        with self._sessions() as session:
            job = session.get(Job, job_id)
            if job is None or job.site_id is None:
                raise JobError(f"job {job_id} has no site")
            site = session.get(Site, job.site_id)
            if site is None or site.deleted_at is not None:
                raise JobError("the site was deleted before discovery started")

            spec = job.spec or {}
            options: dict[str, Any] = {
                "max_pages": int(spec.get("max_pages") or 100),
                "max_depth": int(spec.get("max_depth") or 3),
            }

            # Discovery must see what a reader sees. Without the jar, a flagged
            # blog answers every probe with the interstitial and the picker
            # ends up describing that instead of the blog.
            if site.profile_id is not None:
                temp_dir.mkdir(parents=True, exist_ok=True)
                material = profiles.materialize(session, self._sealer, site.profile_id, temp_dir)
                if material is not None:
                    options["cookies_file"] = str(material.cookies_file)
                    if material.user_agent:
                        options["user_agent"] = material.user_agent

            site.status = "capturing" if site.status == "new" else site.status
            session.commit()
            return {"seed_url": site.seed_url, "site_id": site.id, "options": options}

    def _finish_discovery(self, job_id: int, result: Any, status: str) -> None:
        from cairn.services import discovery_service

        with self._sessions() as session:
            job = session.get(Job, job_id)
            if job is None or job.site_id is None:  # pragma: no cover
                return
            site = session.get(Site, job.site_id)
            if site is None:  # pragma: no cover
                return

            if status == "ok":
                discovery = discovery_service.persist(session, site, result, job_id)
                discovery_service.record_feeds(session, site, result)
                # Only overwrite the scope while it is still the default one.
                # A user who has already picked domains does not want a re-run
                # silently undoing that — the diff is shown instead.
                if not _scope_was_edited(session, site):
                    scope = discovery_service.scope_from_result(result, seed_url=site.seed_url)
                    sites.save_scope(session, site, scope)
                    sites.write_site_yaml(session, self._settings, site)
                job.spec = {**(job.spec or {}), "discovery_id": discovery.id}
                site.status = "ready" if site.last_capture_at else "indexed"
            else:
                site.status = "error"

            job.status = "ok" if status == "ok" else "failed"
            job.finished_at = utcnow()
            job.pid = None
            job.progress = {
                "hosts": len(result.hosts),
                "urls": len(result.all_urls),
                "pages": result.pages_fetched,
            }
            if status != "ok":
                job.error = "; ".join(result.errors[:3]) or "discovery found nothing"
            session.commit()

    def _prepare(self, job_id: int, temp_dir: Path) -> _Prepared:
        """Create the capture row and job directory, and write the job spec."""
        with self._sessions() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise JobError(f"job {job_id} vanished before it started")
            if job.site_id is None:
                raise JobError(f"job {job_id} has no site")
            site = session.get(Site, job.site_id)
            if site is None or site.deleted_at is not None:
                raise JobError("the site was deleted before the capture started")

            engine = self._registry.get(site.engine_id)
            config = engine.validate_config(dict(site.engine_config or {}))

            scope = sites.resolved_scope(session, site)
            # One source of truth at the boundary: the site's politeness wins
            # over the engine defaults, and the engine reads only `config`.
            for key in ("wait_s", "random_wait", "rate_limit"):
                if key in scope.politeness:
                    config[key] = scope.politeness[key]
            config["keep_mirror"] = site.keep_mirror

            kind = str(job.spec.get("kind") or "full")
            started = utcnow()
            dir_name = storage.capture_dir_name(started, kind, site.engine_id)

            capture = Capture(
                site_id=site.id,
                job_id=job.id,
                kind=kind,
                engine_id=site.engine_id,
                engine_version=engine.version,
                dir_name=dir_name,
                started_at=started,
                status="running",
            )
            session.add(capture)
            site.status = "capturing"
            session.flush()

            output_dir = storage.ensure_capture_dirs(self._settings, site.archive_path, dir_name)
            temp_dir.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                temp_dir.chmod(0o700)

            # Every URL discovery found, handed over up front. This is what
            # makes link-following a supplement rather than the way content is
            # found, and it is the reason a deep archive page cannot be out of
            # reach the way it was under a depth limit (docs/04, docs/05).
            from cairn.services import discovery_service

            discovered, seed_source = discovery_service.seeds_for_capture(session, site, scope)
            seeds = list(dict.fromkeys([*discovered, *(job.spec.get("extra_seeds") or [])]))
            seed_source["manual"] = len(job.spec.get("extra_seeds") or []) + 1
            seed_path = temp_dir / SEED_FILE
            storage.write_atomic(seed_path, "\n".join(seeds) + "\n")

            auth: dict[str, Any] = {"user_agent": config.get("user_agent"), "headers": {}}
            if site.profile_id is not None:
                material = profiles.materialize(session, self._sealer, site.profile_id, temp_dir)
                if material is not None:
                    auth["cookies_file"] = str(material.cookies_file)
                    if material.user_agent:
                        auth["user_agent"] = material.user_agent

            spec = {
                "protocol": PROTOCOL_VERSION,
                "job_id": job.id,
                "job_type": job.type,
                "site": {"id": site.id, "slug": site.slug, "title": site.title},
                "output_dir": str(output_dir),
                "temp_dir": str(temp_dir),
                "seeds": seeds,
                "seed_file": SEED_FILE,
                "scope": scope.to_dict(),
                "auth": auth,
                "incremental": {"dedup_cdx": _dedup_cdx(self._settings, session, site, kind)},
                "config": config,
                "limits": {
                    "max_bytes": scope.max_bytes,
                    "max_duration_s": None,
                    "free_space_floor_bytes": None,
                },
            }
            storage.write_json(temp_dir / JOB_SPEC_FILE, spec)

            job.spec = {**job.spec, "capture_id": capture.id, "dir_name": dir_name}
            session.commit()

            return _Prepared(
                job_id=job.id,
                capture_id=capture.id,
                site_id=site.id,
                archive_path=site.archive_path,
                dir_name=dir_name,
                command=[*engine.command, str(temp_dir / JOB_SPEC_FILE)],
                env=engine.env_overrides(),
                output_dir=output_dir,
                temp_dir=temp_dir,
                max_pages=scope.max_pages,
                seeds=seeds,
                seed_source=seed_source,
            )

    async def _execute(self, running: RunningJob, prepared: _Prepared) -> None:
        job_id = prepared.job_id
        self._bus.publish(
            job_id, EV_STATUS, {"status": "running", "capture_id": prepared.capture_id}
        )

        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")
        # Built-in engines need `cairn` importable in a fresh interpreter;
        # drop-ins get nothing extra. See Engine.env_overrides.
        env.update(prepared.env)

        proc = await asyncio.create_subprocess_exec(
            *prepared.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(prepared.temp_dir),
            env=env,
            limit=LINE_LIMIT,
            # Its own process group, so cancelling a job cannot signal the
            # whole container.
            start_new_session=True,
        )
        running.process = proc
        await asyncio.to_thread(self._record_pid, job_id, proc.pid)

        collector = _Collector(
            supervisor=self,
            prepared=prepared,
            bus=self._bus,
        )
        stderr_task = asyncio.create_task(_read_stderr(proc))

        assert proc.stdout is not None
        try:
            while True:
                try:
                    line = await proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    # One absurdly long line; skip it rather than end the crawl.
                    collector.malformed += 1
                    continue
                if not line:
                    break
                await collector.handle(line.decode("utf-8", errors="replace"))
                if prepared.max_pages and collector.url_count >= prepared.max_pages:
                    collector.note(f"page cap of {prepared.max_pages} reached; stopping")
                    running.cancelled = True
                    self._signal(running, signal.SIGTERM)
        finally:
            await collector.flush()

        returncode = await proc.wait()
        stderr = await stderr_task

        status = _final_status(collector.result, returncode, running.cancelled)
        await asyncio.to_thread(
            self._finalize, prepared, collector, status, returncode, stderr, running.cancelled
        )

    # ── terminal states ──────────────────────────────────────────────────

    def _record_pid(self, job_id: int, pid: int) -> None:
        with self._sessions() as session:
            job = session.get(Job, job_id)
            if job is not None:
                job.pid = pid
                session.commit()

    def _finalize(
        self,
        prepared: _Prepared,
        collector: _Collector,
        status: str,
        returncode: int,
        stderr: str,
        cancelled: bool,
    ) -> None:
        with self._sessions() as session:
            job = session.get(Job, prepared.job_id)
            capture = session.get(Capture, prepared.capture_id)
            site = session.get(Site, prepared.site_id)
            if job is None or capture is None or site is None:  # pragma: no cover
                return

            finished = utcnow()
            capture.status = status
            capture.finished_at = finished
            capture.url_count = collector.url_count
            capture.error_count = collector.error_count
            capture.bytes_written = collector.bytes_written
            capture.warc_files = collector.artifacts

            job.status = (
                "ok" if status in ("ok", "partial") else ("cancelled" if cancelled else "failed")
            )
            job.finished_at = finished
            job.pid = None
            job.progress = {
                "done": collector.url_count,
                "bytes": collector.bytes_written,
                "status": status,
            }
            if status == "failed":
                # Both halves, not whichever came first. The engine's summary
                # ("wget exited 1") says what happened; the tool's own stderr
                # says why, and an operator holding only the first is stuck.
                job.error = _failure_text(collector.error, stderr, returncode)

            site.status = "ready" if status in ("ok", "partial") else "error"
            if status in ("ok", "partial"):
                site.last_capture_at = finished
            session.flush()

            if collector.malformed:
                log.warning(
                    "engine emitted unparseable lines",
                    extra={"job": prepared.job_id, "count": collector.malformed},
                )

            try:
                postprocess.run_chain(
                    session,
                    self._settings,
                    capture=capture,
                    site=site,
                    output_dir=prepared.output_dir,
                    tool_version=collector.tool_version,
                    stats=collector.stats,
                    scope=sites.resolved_scope(session, site).to_dict(),
                    seeds=prepared.seeds or [site.seed_url],
                    seed_source=prepared.seed_source,
                )
            except Exception:
                log.exception("post-processing failed", extra={"capture": capture.id})

            sites.write_site_yaml(session, self._settings, site)
            session.commit()

        self._bus.publish(
            prepared.job_id,
            EV_STATUS,
            {
                "status": status,
                "capture_id": prepared.capture_id,
                "stats": collector.stats,
                "error": collector.error,
            },
        )

    def _fail(self, job_id: int, capture_id: int | None, message: str) -> None:
        with self._sessions() as session:
            job = session.get(Job, job_id)
            if job is not None:
                job.status = "cancelled" if message == "cancelled" else "failed"
                job.error = message
                job.finished_at = utcnow()
                job.pid = None
            if capture_id is not None:
                capture = session.get(Capture, capture_id)
                if capture is not None:
                    capture.status = "cancelled" if message == "cancelled" else "failed"
                    capture.finished_at = utcnow()
                    site = session.get(Site, capture.site_id)
                    if site is not None:
                        site.status = "error"
            session.commit()
        self._bus.publish(job_id, EV_STATUS, {"status": "failed", "error": message})


@dataclass(slots=True)
class _Prepared:
    job_id: int
    capture_id: int
    site_id: int
    archive_path: str
    dir_name: str
    command: list[str]
    env: dict[str, str]
    output_dir: Path
    temp_dir: Path
    max_pages: int | None
    seeds: list[str] = field(default_factory=list)
    seed_source: dict[str, int] = field(default_factory=dict)


class _Collector:
    """Turns one engine's event stream into rows, bus traffic, and totals."""

    def __init__(self, supervisor: JobSupervisor, prepared: _Prepared, bus: EventBus) -> None:
        self._supervisor = supervisor
        self._prepared = prepared
        self._bus = bus

        self.url_count = 0
        self.error_count = 0
        self.revisit_count = 0
        self.bytes_written = 0
        self.malformed = 0
        self.tool_version: str | None = None
        self.result: ResultEvent | None = None
        self.error: str | None = None
        self.stats: dict[str, Any] = {}
        self.artifacts: list[dict[str, Any]] = []
        self.seeds: list[str] = []

        self._pending: list[dict[str, Any]] = []
        self._last_flush = time.monotonic()
        self._last_progress_write = 0.0

    async def handle(self, line: str) -> None:
        event = parse_event(line)
        if event is None:
            if line.strip():
                self.malformed += 1
            return

        job_id = self._prepared.job_id

        if isinstance(event, StartedEvent):
            self.tool_version = event.tool_version
            self._bus.publish(job_id, EV_LOG, {"level": "info", "msg": "engine started"})

        elif isinstance(event, LogEvent):
            self._bus.publish(job_id, EV_LOG, {"level": event.level, "msg": event.msg})

        elif isinstance(event, UrlEvent):
            self.url_count += 1
            if event.revisit:
                self.revisit_count += 1
            failed = bool(event.error) or (event.status is not None and event.status >= 400)
            if failed:
                self.error_count += 1
            # Every URL streams, not only the failures. A progress counter
            # ticking beside an empty log box reads as a stall, and watching
            # the crawl move is the entire point of the live view. Volume is
            # bounded at both ends: politeness limits the rate, the subscriber
            # queue is capped, and the client keeps only the last 2000 lines.
            self._bus.publish(
                job_id,
                EV_URL,
                {
                    "url": event.url,
                    "status": event.status,
                    "error": event.error,
                    "mime": event.mime,
                },
            )
            self._pending.append(
                {
                    "capture_id": self._prepared.capture_id,
                    "url": event.url[:2048],
                    "host": _host_of(event.url),
                    "status_code": event.status,
                    "mime": event.mime,
                    "size_bytes": event.size,
                    "digest": event.digest,
                    "is_revisit": event.revisit,
                    "fetched_at": event.ts or utcnow(),
                    "error": event.error,
                }
            )
            await self._maybe_flush()

        elif isinstance(event, ProgressEvent):
            self.bytes_written = event.bytes or self.bytes_written
            self._bus.publish(
                job_id,
                EV_PROGRESS,
                {
                    "done": event.done,
                    "total": event.total,
                    "bytes": event.bytes,
                    "rate_bps": event.rate_bps,
                    "eta_s": event.eta_s,
                },
            )
            await self._write_progress(event)

        elif isinstance(event, ArtifactEvent):
            self._record_artifact(event)

        elif isinstance(event, WarningEvent):
            self._bus.publish(
                job_id,
                EV_WARNING,
                {"code": event.code, "msg": event.msg, "url": event.url},
            )

        elif isinstance(event, ResultEvent):
            self.result = event
            self.stats = dict(event.stats)
            self.error = event.error
            if event.stats.get("bytes"):
                self.bytes_written = int(event.stats["bytes"])
            if event.stats.get("revisits"):
                self.revisit_count = int(event.stats["revisits"])

    def note(self, message: str) -> None:
        self._bus.publish(self._prepared.job_id, EV_LOG, {"level": "warning", "msg": message})

    def _record_artifact(self, event: ArtifactEvent) -> None:
        """Record an artifact, refusing any path that leaves the capture dir.

        The engine is a subprocess whose output is data, not instruction. A
        relative path with enough `..` in it would otherwise have core
        checksum — and later serve — a file anywhere on disk.
        """
        try:
            resolved = storage.resolve_within(self._prepared.output_dir, event.path)
        except StoragePathError:
            log.warning(
                "engine reported an artifact outside its output directory",
                extra={"job": self._prepared.job_id, "path": event.path},
            )
            self.malformed += 1
            return
        if not resolved.is_file():
            log.warning(
                "engine reported an artifact that is not on disk",
                extra={"job": self._prepared.job_id, "path": event.path},
            )
            return
        self.artifacts.append(
            {
                "name": event.path,
                "kind": event.kind,
                "size": event.size if event.size is not None else resolved.stat().st_size,
                "sha256": event.sha256,
            }
        )
        self._bus.publish(
            self._prepared.job_id, EV_ARTIFACT, {"kind": event.kind, "path": event.path}
        )

    async def _maybe_flush(self) -> None:
        """Flush on size or age.

        Size alone starves a slow crawl — a polite 1 req/s capture would hold
        rows in memory for eight minutes before the first write, and lose them
        all if the container stopped.
        """
        aged = (time.monotonic() - self._last_flush) >= URL_BATCH_SECONDS
        if len(self._pending) >= URL_BATCH_SIZE or aged:
            await self.flush()

    async def flush(self) -> None:
        if not self._pending:
            return
        batch, self._pending = self._pending, []
        self._last_flush = time.monotonic()
        await asyncio.to_thread(self._insert, batch)

    def _insert(self, batch: list[dict[str, Any]]) -> None:
        from cairn.db.models import CaptureUrl

        with self._supervisor._sessions() as session:
            session.bulk_insert_mappings(CaptureUrl.__mapper__, batch)
            session.commit()

    async def _write_progress(self, event: ProgressEvent) -> None:
        now = time.monotonic()
        if (now - self._last_progress_write) < PROGRESS_WRITE_SECONDS:
            return
        self._last_progress_write = now
        payload = {
            "done": event.done,
            "total": event.total,
            "bytes": event.bytes,
            "eta_s": event.eta_s,
        }
        await asyncio.to_thread(self._store_progress, payload)

    def _store_progress(self, payload: dict[str, Any]) -> None:
        with self._supervisor._sessions() as session:
            job = session.get(Job, self._prepared.job_id)
            if job is not None:
                job.progress = payload
                session.commit()


# ── helpers ──────────────────────────────────────────────────────────────


def _final_status(result: ResultEvent | None, returncode: int, cancelled: bool) -> str:
    """Reconcile the engine's claim with how it actually exited.

    An engine that says `ok` then exits non-zero does not get to be ok, and one
    that exits 0 without a `result` line never reported a terminal state, which
    the contract makes a failure (docs/05).
    """
    if cancelled:
        return "cancelled" if result is None else "partial"
    if result is None:
        return "failed"
    if returncode != 0:
        return "partial" if result.status == "ok" else result.status
    return result.status


def _scope_was_edited(session: Session, site: Site) -> bool:
    """Whether the user has picked domains themselves.

    A re-run of discovery must not silently undo a deliberate selection. The
    flag is set by the scope endpoint rather than inferred, because "differs
    from the default" would also be true of a scope discovery itself wrote.
    """
    return bool((site.scope_settings or {}).get("user_edited"))


def _failure_text(engine_error: str | None, stderr: str, returncode: int) -> str:
    """Combine the engine's summary with the tool's own diagnostic output."""
    parts = [engine_error or f"engine exited {returncode}"]
    tail = stderr.strip()
    if tail:
        parts.append(tail[-2000:])
    return "\n\n".join(parts)


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "")[:255]


def _dedup_cdx(settings: Settings, session: Session, site: Site, kind: str) -> str | None:
    """The previous capture's CDX, for `--warc-dedup`.

    Only for incremental runs: a full capture is meant to stand alone, and
    deduping it against an older one produces revisit records pointing at an
    archive the user may later delete.
    """
    if kind == "full":
        return None
    previous = session.scalars(
        select(Capture)
        .where(Capture.site_id == site.id, Capture.status.in_(("ok", "partial")))
        .order_by(Capture.started_at.desc())
        .limit(1)
    ).first()
    if previous is None:
        return None
    candidate = (
        storage.site_dir(settings, site.archive_path)
        / storage.CAPTURES_DIR
        / previous.dir_name
        / storage.WARC_DIR
        / "part.cdx"
    )
    return str(candidate) if candidate.exists() else None


async def _read_stderr(proc: asyncio.subprocess.Process) -> str:
    """Drain stderr into a bounded buffer.

    Bounded because an engine in a retry loop can produce megabytes of the
    same message, and it is shown in the UI on failure.
    """
    if proc.stderr is None:  # pragma: no cover
        return ""
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await proc.stderr.read(4096)
        if not chunk:
            break
        if size < STDERR_LIMIT:
            chunks.append(chunk)
            size += len(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _remove_tree(path: Path) -> None:
    import shutil

    with contextlib.suppress(OSError):
        shutil.rmtree(path)
