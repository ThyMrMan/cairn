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
import json
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
from cairn.services import postprocess, profiles, settings_store, sites, storage
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

PER_HOST_SERIAL_SETTING = "jobs.per_host_serial"
# Whether engines may start sibling containers. Defaults to *true*: mounting
# the Docker socket is itself the deliberate act, and a second switch that
# defaults to off would mean somebody mounts the socket, picks a container
# engine, and is told it is unavailable with no hint why. It exists so an
# operator who mounted the socket for something else can still forbid this.
ALLOW_DOCKER_SETTING = "jobs.allow_docker"


class JobError(RuntimeError):
    """A job could not be prepared or started."""


@dataclass(slots=True)
class RunningJob:
    job_id: int
    process: asyncio.subprocess.Process | None = None
    # Set instead of `process` when the engine runs as a sibling container.
    # Cancellation has to reach whichever one is actually doing the work.
    container: str | None = None
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
        await self._sweep_containers()
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

    async def _sweep_containers(self) -> None:
        """Remove engine containers a previous process left running.

        The subprocess path gets this for free — a child dies with its parent.
        A sibling container does not: our process can be killed mid-capture and
        the crawler keeps going, holding the archive open and writing into a
        capture that the database already calls interrupted.
        """
        from cairn.services import containers

        ok, _reason = containers.available()
        if not ok:
            return
        with contextlib.suppress(Exception):
            async with containers.client() as http:
                await containers.sweep(http)

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
        if running.container is not None:
            from cairn.services import containers

            async with containers.client() as http:
                await containers.stop(http, running.container)
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
        """Move up to `capacity` queued jobs to running, in priority order.

        Skipping rather than blocking: a job held back by the per-host rule
        leaves its place in the queue and the next candidate is considered, so
        one busy host cannot stall work on every other site.
        """
        with self._sessions() as session:
            serial = settings_store.get(session, PER_HOST_SERIAL_SETTING, True)
            busy = _busy_hosts(session) if serial else set()

            # More candidates than capacity, because some will be skipped.
            rows = session.scalars(
                select(Job)
                .where(Job.status == "queued")
                .order_by(Job.priority.asc(), Job.queued_at.asc())
                .limit(max(capacity * 8, 16))
            ).all()

            claimed: list[int] = []
            for job in rows:
                if len(claimed) >= capacity:
                    break
                host = _host_for_job(session, job) if serial else None
                if host is not None and host in busy:
                    continue
                if host is not None:
                    busy.add(host)
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
            if job_type == "move":
                await self._run_move(running)
                return
            if job_type in ("export", "verify", "index"):
                await self._run_maintenance(running, job_type)
                return
            # Before the capture, never during it: see _refresh_stale_profile.
            await self._refresh_stale_profile(job_id)
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

    # ── keeping a profile fresh ──────────────────────────────────────────

    async def _refresh_stale_profile(self, job_id: int) -> None:
        """Re-mint an aging jar before the crawl, not during it.

        docs/06 describes the supervisor pausing a running capture on
        `interstitial_detected`, re-minting, and resuming with the fresh jar.
        **wget cannot do that.** It reads `--load-cookies` once at startup and
        holds the jar in memory; there is no path to hand a running crawl a
        new one, and writing over the file it was given changes nothing.

        So the check moves to the only place it can do any good: before the
        job starts. A cookie that would have expired two hours into a six-hour
        crawl is re-minted while re-minting is still free, which covers the
        case the pause-and-resume design was aiming at. What is left over —
        a jar that dies mid-crawl anyway — is detected afterwards by the
        capture's own gap report, because by then the only honest thing to do
        is say so rather than pretend the archive is complete.
        """
        plan = await asyncio.to_thread(self._mint_plan, job_id)
        if plan is None:
            return

        self._bus.publish(
            job_id,
            EV_LOG,
            {"level": "info", "msg": f"Re-minting the access profile first: {plan['why']}"},
        )
        try:
            from cairn.services import mint as mint_service

            result = await mint_service.mint(
                script_text=plan["script"],
                verify_url=plan["verify_url"],
                user_agent=plan["user_agent"],
            )
        except Exception as exc:
            # Never fatal. The jar may still work, and refusing to start the
            # capture would turn a possible problem into a certain one.
            self._bus.publish(
                job_id,
                EV_LOG,
                {
                    "level": "warning",
                    "msg": f"Could not re-mint the profile: {str(exc).splitlines()[0][:200]}",
                },
            )
            return

        await asyncio.to_thread(self._store_mint, plan["profile_id"], result)
        self._bus.publish(
            job_id,
            EV_LOG,
            {
                "level": "info" if result.ok else "warning",
                "msg": (
                    f"Profile re-minted: {result.reason}"
                    if result.ok
                    else f"Re-mint failed, continuing with the jar we have: {result.reason}"
                ),
            },
        )

    def _mint_plan(self, job_id: int) -> dict[str, Any] | None:
        """Whether this job's profile needs re-minting, and what it would take."""
        from datetime import timedelta

        from cairn.db.models import AccessProfile
        from cairn.services import profiles as profile_service
        from cairn.services import settings_store

        with self._sessions() as session:
            job = session.get(Job, job_id)
            if job is None or job.site_id is None:
                return None
            site = session.get(Site, job.site_id)
            if site is None or site.profile_id is None:
                return None
            profile = session.get(AccessProfile, site.profile_id)
            # Only a userscript profile can be re-minted unattended: it is the
            # only mode that still holds the thing that does the minting.
            if profile is None or profile.mode != "userscript" or not profile.verify_url:
                return None

            script = profile_service.load_script(self._sealer, profile)
            if script is None:
                return None

            now = utcnow()
            ttl_days = settings_store.get_int(session, "profiles.mint_ttl_days", 7)
            why: str | None = None
            if profile.cookies_enc is None:
                why = "there is no jar yet"
            elif profile.expires_at is not None and profile.expires_at <= now + timedelta(hours=24):
                why = "the earliest cookie expires within a day"
            elif profile.minted_at is None or profile.minted_at <= now - timedelta(days=ttl_days):
                why = f"the jar is older than {ttl_days} days"
            if why is None:
                return None

            return {
                "profile_id": profile.id,
                "script": script,
                "verify_url": profile.verify_url,
                "user_agent": profile.user_agent,
                "why": why,
            }

    def _store_mint(self, profile_id: int, result: Any) -> None:
        from cairn.db.models import AccessProfile
        from cairn.services import profiles as profile_service

        with self._sessions() as session:
            profile = session.get(AccessProfile, profile_id)
            if profile is None:  # pragma: no cover — deleted mid-job
                return
            if result.ok and result.cookies_text:
                profile_service.store_cookies(
                    session, self._sealer, profile, result.cookies_text, mode="userscript"
                )
            profile.last_verified_at = utcnow()
            profile.last_verify_result = "ok" if result.ok else "failed"
            session.commit()

    # ── moves ────────────────────────────────────────────────────────────

    async def _run_move(self, running: RunningJob) -> None:
        """Relocate an archive across filesystems.

        Only reached when a rename was impossible — every ordinary move
        happens inside the request that asked for it, because it is one
        `rename(2)` and finishing it in a job would mean showing a progress bar
        for an operation that already completed. What lands here is the copy:
        long enough to want cancelling, and much more likely to be interrupted
        by the container stopping, which is what the job machinery is for.
        """
        job_id = running.job_id
        self._bus.publish(job_id, EV_STATUS, {"status": "running"})
        self._bus.publish(
            job_id,
            EV_LOG,
            {
                "level": "info",
                "msg": "The archive is moving to a different filesystem, so it has to be "
                "copied rather than renamed. This takes as long as the data is large.",
            },
        )
        result = await asyncio.to_thread(self._execute_move, job_id)
        self._bus.publish(job_id, EV_STATUS, {"status": result.get("status", "ok"), **result})

    def _execute_move(self, job_id: int) -> dict[str, Any]:
        from cairn.services import folders, moves

        with self._sessions() as session:
            job = session.get(Job, job_id)
            if job is None:  # pragma: no cover
                return {"status": "failed", "error": "the job vanished"}
            spec = job.spec or {}
            op = str(spec.get("op") or "")

            try:
                if op == "move-site":
                    site = session.get(Site, int(spec["site_id"]))
                    target = folders.require_folder(session, int(spec["target_folder_id"]))
                    if site is None:
                        raise moves.MoveError("the site was deleted before the move started")
                    outcome = moves.move_site(session, self._settings, site, target, copy_ok=True)
                elif op == "rename-folder":
                    folder = folders.require_folder(session, int(spec["folder_id"]))
                    plan = folders.plan_rename(
                        session, self._settings, folder, name=str(spec["name"])
                    )
                    outcome = moves.relocate(session, self._settings, plan, copy_ok=True)
                elif op == "reparent-folder":
                    folder = folders.require_folder(session, int(spec["folder_id"]))
                    parent = spec.get("parent_id")
                    plan = folders.plan_reparent(
                        session,
                        self._settings,
                        folder,
                        parent_id=int(parent) if parent is not None else None,
                    )
                    outcome = moves.relocate(session, self._settings, plan, copy_ok=True)
                else:  # pragma: no cover — the API is the only producer
                    raise moves.MoveError(f"unknown move operation {op!r}")
            except (moves.MoveError, folders.FolderError, OSError) as exc:
                job.status = "failed"
                job.error = str(exc)
                job.finished_at = utcnow()
                session.commit()
                return {"status": "failed", "error": str(exc)}

            job.status = "ok"
            job.finished_at = utcnow()
            job.progress = {"method": outcome.method, "sites": len(outcome.site_ids)}
            session.commit()
            return {
                "status": "ok",
                "method": outcome.method,
                "from": outcome.old_path,
                "to": outcome.new_path,
            }

    # ── export, verify, reindex ──────────────────────────────────────────
    #
    # In-process like discovery and move, and for the same reason: none of
    # them runs a crawler, all of them need the database, and putting them
    # through the queue is what makes them cancellable, restart-recoverable
    # and visible in the same job list as everything else.

    async def _run_maintenance(self, running: RunningJob, job_type: str) -> None:
        job_id = running.job_id
        self._bus.publish(job_id, EV_STATUS, {"status": "running"})

        def say(message: str) -> None:
            self._bus.publish(job_id, EV_LOG, {"level": "info", "msg": message})

        def step(done: int, total: int, label: str) -> None:
            self._bus.publish(job_id, EV_PROGRESS, {"done": done, "total": total, "url": label})

        runner = {
            "export": self._execute_export,
            "verify": self._execute_verify,
            "index": self._execute_reindex,
        }[job_type]
        result = await asyncio.to_thread(runner, job_id, say, step)

        findings = int(result.get("findings") or 0)
        if job_type == "verify" and findings:
            from cairn.services import notify

            await notify.send(
                self._sessions,
                notify.INTEGRITY_MISMATCH,
                title="Archive integrity check found a problem",
                body=f"{findings} finding(s) across {result.get('captures', 0)} capture(s). "
                "Nothing was changed — open Archive health to see what.",
                priority="high",
            )
        self._bus.publish(job_id, EV_STATUS, result)

    def _execute_export(self, job_id: int, say: Any, step: Any) -> dict[str, Any]:
        from cairn.services import wacz

        with self._sessions() as session:
            job = session.get(Job, job_id)
            if job is None or job.site_id is None:  # pragma: no cover
                return {"status": "failed", "error": "the job has no site"}
            site = session.get(Site, job.site_id)
            if site is None or site.deleted_at is not None:
                return self._finish_job(session, job, "failed", "the site was deleted")

            spec = job.spec or {}
            capture_dirs = [str(d) for d in (spec.get("capture_dirs") or [])] or None
            target = wacz.exports_dir(self._settings, site.archive_path) / (
                str(spec.get("filename") or wacz.export_name(site.slug))
            )
            say(
                f"Packaging {'this capture' if capture_dirs else 'every capture'} of "
                f"{site.title} into {target.name}."
            )
            done = [0]

            def progress(name: str) -> None:
                done[0] += 1
                step(done[0], 0, name)

            try:
                result = wacz.build(
                    self._settings,
                    archive_path=site.archive_path,
                    target=target,
                    capture_dirs=capture_dirs,
                    title=site.title,
                    description=f"Archived from {site.seed_url} by cairn.",
                    main_page_url=site.seed_url,
                    progress=progress,
                )
            except (wacz.WaczError, OSError, storage.StoragePathError) as exc:
                return self._finish_job(session, job, "failed", str(exc))

            say(
                f"Wrote {result.path.name}: {result.warcs} WARC(s), {result.records} records, "
                f"{result.pages} page(s), {result.size_bytes / 1024 / 1024:.1f} MB."
            )
            return self._finish_job(
                session,
                job,
                "ok",
                None,
                progress={
                    "file": result.path.name,
                    "bytes": result.size_bytes,
                    "records": result.records,
                    "pages": result.pages,
                    "warcs": result.warcs,
                },
            )

    def _execute_verify(self, job_id: int, say: Any, step: Any) -> dict[str, Any]:
        from cairn.services import integrity

        with self._sessions() as session:
            job = session.get(Job, job_id)
            if job is None:  # pragma: no cover
                return {"status": "failed", "error": "the job vanished"}
            spec = job.spec or {}
            deep = bool(spec.get("deep"))
            say(
                "Re-reading every archived file and comparing it to the checksum taken when "
                "it was written." + (" Parsing each WARC as well." if deep else "")
            )
            report = integrity.verify(
                session,
                self._settings,
                site_id=job.site_id,
                deep=deep,
                progress=lambda done, total, label: step(done, total, label),
            )
            # Only a whole-archive pass may claim to be the archive's state.
            if job.site_id is None:
                integrity.save(self._settings, report)

            if report.ok:
                say(
                    f"{report.files} file(s) across {report.captures} capture(s) verified, "
                    f"{report.bytes_read / 1024 / 1024:.0f} MB read. Nothing has changed."
                )
            else:
                for finding in report.findings[:20]:
                    self._bus.publish(
                        job_id,
                        EV_LOG,
                        {
                            "level": "error",
                            "msg": f"{finding.kind}: {finding.file or ''} "
                            f"({finding.site_title}) — {finding.detail}",
                        },
                    )

            return self._finish_job(
                session,
                job,
                "ok",
                None,
                progress={
                    "files": report.files,
                    "captures": report.captures,
                    "findings": len(report.findings),
                    "bytes_read": report.bytes_read,
                    "ok": report.ok,
                },
            )

    def _execute_reindex(self, job_id: int, say: Any, step: Any) -> dict[str, Any]:
        from cairn.services import search, textextract

        with self._sessions() as session:
            job = session.get(Job, job_id)
            if job is None:  # pragma: no cover
                return {"status": "failed", "error": "the job vanished"}
            spec = job.spec or {}
            extract = bool(spec.get("extract"))

            targets = list(
                session.scalars(
                    select(Site).where(
                        Site.deleted_at.is_(None),
                        *([Site.id == job.site_id] if job.site_id else []),
                    )
                ).all()
            )
            say(
                f"Rebuilding the search index for {len(targets)} site(s)"
                + (
                    ", re-reading the WARCs to extract text again."
                    if extract
                    else " from the text already extracted."
                )
            )

            total = 0
            for n, site in enumerate(targets, start=1):
                step(n, len(targets), site.title)
                if extract:
                    for capture in session.scalars(
                        select(Capture).where(Capture.site_id == site.id)
                    ).all():
                        textextract.extract_capture(
                            self._settings, site.archive_path, capture.dir_name
                        )
                total += search.reindex_site(session, self._settings, site)
                session.commit()

            say(f"{total} page(s) indexed.")
            return self._finish_job(
                session, job, "ok", None, progress={"pages": total, "sites": len(targets)}
            )

    def _finish_job(
        self,
        session: Session,
        job: Job,
        status: str,
        error: str | None,
        progress: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        job.status = status
        job.error = error
        job.finished_at = utcnow()
        if progress is not None:
            job.progress = progress
        session.commit()
        return {"status": status, **({"error": error} if error else {}), **(progress or {})}

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

            kind = str(job.spec.get("kind") or "full")
            scope = sites.resolved_scope(session, site)
            # A feed capture is about the posts it was given, not about the
            # site. Left at the site's depth it would follow the new post's
            # link to the archive index and re-crawl everything, which is
            # exactly the cost this is meant to avoid (docs/08).
            #
            # `--level=1` reads ambiguously, so it was measured against wget
            # 1.25.0: a seed linking to an index which links to an old post
            # fetches the seed, its requisites and the index — and stops. Not
            # the seed alone, and not the archive behind the index. That is
            # what docs/08's prose asks for, so the number matches the intent.
            if job.spec.get("max_depth") is not None:
                scope.max_depth = int(job.spec["max_depth"])

            warnings: list[str] = []
            if sites.scope_is_unindexed(session, site):
                warnings.append(
                    "This site has never been indexed, so the capture ran with the "
                    "default scope: the seed host and nothing else. Everything the "
                    "pages pull from elsewhere — images on a CDN, the theme's CSS "
                    "and JS — was out of scope and is not in this archive. Press "
                    "Index on the site, check the domain list, and capture again."
                )
            # One source of truth at the boundary: the site's politeness wins
            # over the engine defaults, and the engine reads only `config`.
            for key in ("wait_s", "random_wait", "rate_limit"):
                if key in scope.politeness:
                    config[key] = scope.politeness[key]
            config["keep_mirror"] = site.keep_mirror

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

            extra = list(job.spec.get("extra_seeds") or [])
            if job.spec.get("only_extra_seeds"):
                # The whole discovered URL set is what makes a *full* capture
                # complete; handing it to an incremental one turns "archive
                # this new post" back into "archive the site". The seed URL is
                # not added either — the point is these URLs and nothing else.
                seeds = list(dict.fromkeys(extra))
                seed_source = {"feed": len(seeds)}
            else:
                discovered, seed_source = discovery_service.seeds_for_capture(session, site, scope)
                seeds = list(dict.fromkeys([*discovered, *extra]))
                seed_source["manual"] = len(extra) + 1
            seed_path = temp_dir / SEED_FILE
            storage.write_atomic(seed_path, "\n".join(seeds) + "\n")

            auth: dict[str, Any] = {"user_agent": config.get("user_agent"), "headers": {}}
            if site.profile_id is not None:
                material = profiles.materialize(session, self._sealer, site.profile_id, temp_dir)
                if material is not None:
                    auth["cookies_file"] = str(material.cookies_file)
                    if material.user_agent:
                        auth["user_agent"] = material.user_agent
                warnings.extend(_credential_warnings(session, site.profile_id))

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
                "incremental": {
                    "dedup_cdx": _dedup_cdx(self._settings, session, site, kind, temp_dir)
                },
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
                warnings=warnings,
                site_title=site.title,
                runtime=dict(engine.runtime),
                feed_id=job.spec.get("feed_id"),
                item_ids=[int(i) for i in (job.spec.get("item_ids") or [])],
            )

    async def _execute(self, running: RunningJob, prepared: _Prepared) -> None:
        job_id = prepared.job_id
        self._bus.publish(
            job_id, EV_STATUS, {"status": "running", "capture_id": prepared.capture_id}
        )
        for warning in prepared.warnings:
            self._bus.publish(job_id, EV_LOG, {"level": "warning", "msg": warning})

        if prepared.runtime.get("type") == "docker":
            await self._execute_container(running, prepared)
            return

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
        await self._announce(prepared, collector, status)

    async def _execute_container(self, running: RunningJob, prepared: _Prepared) -> None:
        """Run an engine image beside us and read its stdout as the protocol.

        The container half of the same contract: the image is handed a job
        spec and writes cairn NDJSON, exactly as a subprocess engine does. What
        differs is that its filesystem is not ours, so the spec it receives
        names the two directories it can actually see.
        """
        from cairn.services import containers

        job_id = prepared.job_id
        collector = _Collector(supervisor=self, prepared=prepared, bus=self._bus)

        ok, reason = containers.available()
        if not ok or not await asyncio.to_thread(self._docker_allowed):
            await asyncio.to_thread(
                self._fail,
                job_id,
                prepared.capture_id,
                reason
                or "Container engines are switched off for this instance (jobs.allow_docker).",
            )
            return

        # The spec the container reads names *its* paths, not ours. Written
        # over the one `_prepare` wrote, because a capture uses one runtime and
        # two specs in one directory is an invitation to read the wrong one.
        spec_path = prepared.temp_dir / JOB_SPEC_FILE
        storage.write_json(
            spec_path,
            containers.rewrite_spec(json.loads(spec_path.read_text(encoding="utf-8"))),
        )

        image = str(prepared.runtime.get("image") or "")
        argv = [str(a) for a in (prepared.runtime.get("args") or [])] or [
            f"{containers.CONTAINER_JOB}/{JOB_SPEC_FILE}"
        ]
        run = containers.RunSpec(
            image=image,
            argv=argv,
            mounts=[
                (prepared.output_dir, containers.CONTAINER_OUT),
                (prepared.temp_dir, containers.CONTAINER_JOB),
            ],
            env={str(k): str(v) for k, v in (prepared.runtime.get("env") or {}).items()},
            # A protocol-speaking image is handed its job directory as the
            # working directory unless it says otherwise, so a relative path in
            # its own arguments means what the author expected.
            workdir=str(prepared.runtime.get("workdir") or containers.CONTAINER_JOB),
            job_id=job_id,
            shm_size=str(prepared.runtime.get("shm_size") or "2g"),
            memory=str(prepared.runtime.get("memory") or ""),
        )

        stderr = ""
        returncode = -1
        async with containers.client() as http:
            try:
                if not await containers.image_present(http, image):
                    self._bus.publish(job_id, EV_LOG, {"level": "info", "msg": f"pulling {image}"})
                    async for status in containers.pull(http, image):
                        self._bus.publish(job_id, EV_LOG, {"level": "debug", "msg": status})

                running.container = await containers.create(http, run)
                await containers.start(http, running.container)
                stderr = await self._pump_container(http, running, collector)
                returncode = await containers.wait(http, running.container)
            except containers.ContainerError as exc:
                await collector.flush()
                await asyncio.to_thread(self._fail, job_id, prepared.capture_id, str(exc))
                return
            finally:
                await collector.flush()
                if running.container is not None:
                    await containers.remove(http, running.container)

        status = _final_status(collector.result, returncode, running.cancelled)
        await asyncio.to_thread(
            self._finalize, prepared, collector, status, returncode, stderr, running.cancelled
        )
        await self._announce(prepared, collector, status)

    async def _pump_container(self, http: Any, running: RunningJob, collector: _Collector) -> str:
        """Reassemble the container's framed log stream into protocol lines.

        Docker frames each chunk with an eight-byte header and the frames do
        not align with lines, so both streams are buffered separately —
        stdout is the protocol, stderr is diagnostics for the failure message.
        """
        from cairn.services import containers

        assert running.container is not None
        stdout = b""
        errors: list[bytes] = []
        error_bytes = 0

        async for stream, chunk in containers.stream_logs(http, running.container):
            if stream == 2:
                if error_bytes < STDERR_LIMIT:
                    errors.append(chunk)
                    error_bytes += len(chunk)
                continue
            stdout += chunk
            while b"\n" in stdout:
                line, stdout = stdout.split(b"\n", 1)
                await collector.handle(line.decode("utf-8", errors="replace"))
            cap = collector._prepared.max_pages
            if cap and collector.url_count >= cap and not running.cancelled:
                collector.note(f"page cap of {cap} reached; stopping")
                running.cancelled = True
                await containers.stop(http, running.container)
        if stdout.strip():
            await collector.handle(stdout.decode("utf-8", errors="replace"))
        return b"".join(errors).decode("utf-8", errors="replace")

    def _docker_allowed(self) -> bool:
        with self._sessions() as session:
            return bool(settings_store.get(session, ALLOW_DOCKER_SETTING, True))

    async def _announce(self, prepared: _Prepared, collector: _Collector, status: str) -> None:
        """Push what a person would want to hear about, after the fact.

        Deliberately outside `_finalize`: that runs in a worker thread with a
        session open, and a webhook timing out there would hold both for ten
        seconds after the work was already done. Nothing here can change the
        capture's outcome.
        """
        from cairn.services import notify

        if status == "failed":
            await notify.send(
                self._sessions,
                notify.CAPTURE_FAILED,
                title=f"Capture failed: {prepared.site_title}",
                body=(collector.error or "the engine did not say why")[:500],
                priority="high",
            )
        elif status == "partial":
            await notify.send(
                self._sessions,
                notify.INTERSTITIAL_DETECTED,
                title=f"Capture incomplete: {prepared.site_title}",
                body=(
                    f"{collector.url_count} URLs, {collector.error_count} errors. "
                    "Check the capture's gap report — a content warning or an expired "
                    "access profile is the usual cause."
                ),
            )
        elif status == "ok" and prepared.item_ids:
            await notify.send(
                self._sessions,
                notify.ITEMS_CAPTURED,
                title=f"{len(prepared.item_ids)} new item(s) archived: {prepared.site_title}",
                body="\n".join(prepared.seeds[:10]),
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
                    warnings=prepared.warnings,
                )
            except Exception:
                log.exception("post-processing failed", extra={"capture": capture.id})

            if prepared.item_ids:
                _settle_feed_items(session, prepared.item_ids, capture, status)

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
    site_title: str = ""
    # The engine's `runtime` block, which decides whether the work happens in
    # a subprocess of ours or in a container beside us.
    runtime: dict[str, Any] = field(default_factory=dict)
    # Set when a feed poll asked for this capture, so its items can be marked
    # once the capture has actually finished rather than optimistically when
    # it was queued.
    feed_id: int | None = None
    item_ids: list[int] = field(default_factory=list)
    # Problems known before the engine starts. Shown in the live log as the
    # crawl begins, and repeated in the capture's gap report so they survive
    # past the moment the log scrolls away.
    warnings: list[str] = field(default_factory=list)


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


def _credential_warnings(session: Session, profile_id: int) -> list[str]:
    """Say so before the crawl when the jar carries an account session.

    A WARC records the request as well as the response, so every `Cookie:`
    header wget sends is written into the archive. That is standard and not
    suppressible — which means an archive fetched with a full account session
    contains that session, and it travels with any copy or export of the file.

    A consent cookie is not worth mentioning. A Google `SID` is, and the
    profile parser already knows the difference (docs/06, docs/11).
    """
    from cairn.db.models import AccessProfile

    profile = session.get(AccessProfile, profile_id)
    if profile is None:
        return []
    sensitive = list((profile.cookie_meta or {}).get("sensitive") or [])
    if not sensitive:
        return []
    return [
        f"This profile carries account session cookies ({', '.join(sorted(sensitive)[:4])}). "
        "A WARC stores the request headers as well as the responses, so those cookies will "
        "be inside this archive — treat the files as credentials, and prefer a profile "
        "holding only what the gate needs."
    ]


def _settle_feed_items(
    session: Session, item_ids: list[int], capture: Capture, status: str
) -> None:
    """Mark a feed poll's items with what the capture actually did.

    Only on success. An item left pending after a failed capture is retried on
    the next scheduled pass, which is what should happen — marking it captured
    because a job ran would leave a post permanently missing from the archive
    with nothing recording that it was ever meant to be there.
    """
    from cairn.db.models import FeedItem

    if status not in ("ok", "partial"):
        for item in session.scalars(select(FeedItem).where(FeedItem.id.in_(item_ids))).all():
            item.status = "pending"
        return
    for item in session.scalars(select(FeedItem).where(FeedItem.id.in_(item_ids))).all():
        item.status = "captured"
        item.capture_id = capture.id


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


# Job types that make requests to somebody else's server. A move copies bytes
# between two local directories and has no business waiting for a crawl.
NETWORKED_JOB_TYPES = frozenset({"capture", "discovery"})


def _host_for_job(session: Session, job: Job) -> str | None:
    """The host this job will hammer, or None if it will not hammer one.

    Per-host serialization is enforced here rather than in the scheduler
    because it is not a scheduling preference — it is politeness, and a manual
    capture started from the UI owes the same politeness as a scheduled one.
    Two simultaneous crawls of one blog is the thing that gets an archiver
    rate-limited or blocked, and no concurrency setting should be able to
    permit it (docs/08).
    """
    if job.type not in NETWORKED_JOB_TYPES or job.site_id is None:
        return None
    site = session.get(Site, job.site_id)
    return site.primary_host.lower() if site and site.primary_host else None


def _busy_hosts(session: Session) -> set[str]:
    rows = session.execute(
        select(Site.primary_host)
        .join(Job, Job.site_id == Site.id)
        .where(Job.status == "running", Job.type.in_(NETWORKED_JOB_TYPES))
    ).all()
    return {str(host).lower() for (host,) in rows if host}


# A CDX line is ~200 bytes; a site with a hundred thousand archived URLs makes
# a 20 MB file wget reads once. Capped anyway, newest first, so a pathological
# archive degrades to deduplicating against its recent history rather than
# building an unbounded file on every capture.
MAX_DEDUP_LINES = 400_000
CDX_HEADER = " CDX a b a m s k r M V g u"


def _dedup_cdx(
    settings: Settings, session: Session, site: Site, kind: str, temp_dir: Path
) -> str | None:
    """Everything this site has already archived, for `--warc-dedup`.

    Only for incremental runs: a full capture is meant to stand alone, and
    deduping it against an older one produces revisit records pointing at an
    archive the user may later delete.

    **Every prior capture, not the previous one.** wget writes no CDX line for
    a URL it deduplicated, so a capture whose payloads were all unchanged
    leaves an empty `part.cdx` — measured against wget 1.25.0. Handing only
    that file to the next run means it deduplicates against nothing and
    re-stores every byte of the theme, so the saving would hold for exactly
    one capture and then silently stop, alternating full-size runs forever.
    The union of every capture's CDX is the complete set of payloads this site
    already has, which is what the flag actually wants.
    """
    if kind == "full":
        return None

    captures = session.scalars(
        select(Capture)
        .where(Capture.site_id == site.id, Capture.status.in_(("ok", "partial")))
        .order_by(Capture.started_at.desc())
    ).all()
    if not captures:
        return None

    root = storage.site_dir(settings, site.archive_path) / storage.CAPTURES_DIR
    lines: list[str] = []
    seen: set[str] = set()
    for capture in captures:
        source = root / capture.dir_name / storage.WARC_DIR / "part.cdx"
        if not source.exists():
            continue
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover — a capture directory going away mid-job
            continue
        for line in text.splitlines():
            if not line or line.startswith(" CDX"):
                continue
            # Keyed on url+digest: the same URL with a different payload is a
            # different record and both are worth having.
            fields = line.split(" ")
            key = f"{fields[0]}\t{fields[5] if len(fields) > 5 else ''}"
            if key in seen:
                continue
            seen.add(key)
            lines.append(line)
            if len(lines) >= MAX_DEDUP_LINES:
                break
        if len(lines) >= MAX_DEDUP_LINES:
            break

    if not lines:
        return None
    merged = temp_dir / "dedup.cdx"
    storage.write_atomic(merged, "\n".join([CDX_HEADER, *lines]) + "\n")
    return str(merged)


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
