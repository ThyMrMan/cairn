"""The `wget-warc` engine: `python -m cairn.engines.wget <job.json>`.

A thin wrapper — build argv, spawn wget, turn its output into events. It runs
as its own process so a crawl cannot take the web app down with it, and it
speaks only the NDJSON protocol so nothing here is privileged over a
third-party engine.

Two output streams are watched, for different reasons, both established
against wget 1.25.0 rather than assumed:

  - `part.cdx` (`--warc-cdx`) is written incrementally, one line per archived
    record, carrying status, MIME, and payload digest. It is the source for
    `url` events. Its format is space-separated, not tab-separated, and the
    same URL legitimately appears more than once (a redirect target that is
    also fetched directly), so core must tolerate repeats.
  - `crawl.log` carries the failures that never became a record at all —
    connection refused, timeouts, DNS. Those are invisible in the CDX, and
    they are exactly the ones worth showing a user.

Politeness lives in `config`, not `scope`: core folds the site's politeness
settings into the engine config before writing `job.json`, so there is one
source of truth at the boundary rather than two that can disagree.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import FrameType
from typing import Any

from cairn.engines.protocol import SEED_FILE, EventWriter, JobSpec
from cairn.services.scope import Scope, to_wget_args

POLL_INTERVAL_S = 0.25
PROGRESS_INTERVAL_S = 1.0
DEFAULT_GRACE_S = 60

# wget's CDX header line; skipped rather than parsed.
CDX_HEADER_PREFIX = " CDX"
CDX_FIELDS = 11

WGET_BIN = os.environ.get("CAIRN_WGET_BIN", "wget")

# wget exit codes. 0 is clean; 1-3 are local/IO problems; 4-8 mean some URLs
# failed, which on a real site is normal and must not be reported as a failed
# capture (docs/05 — `partial` is a first-class outcome).
EXIT_OK = 0
EXIT_PARTIAL = frozenset({4, 5, 6, 7, 8})


class Tailer:
    """Yields complete lines appended to a file since the last call."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._pos = 0
        self._buffer = ""

    def lines(self) -> Iterator[str]:
        if not self.path.exists():
            return
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self._pos)
                chunk = fh.read()
                self._pos = fh.tell()
        except OSError:  # pragma: no cover — file replaced mid-read
            return
        if not chunk:
            return
        self._buffer += chunk.decode("utf-8", errors="replace")
        parts = self._buffer.split("\n")
        # The trailing element is either empty or an incomplete line; hold it
        # back so a half-written record is never parsed as a whole one.
        self._buffer = parts.pop()
        yield from parts


def parse_cdx_line(line: str) -> dict[str, Any] | None:
    """One wget CDX record → the fields of a `url` event.

    Layout (from the header wget writes): a b a m s k r M V g u —
    url, timestamp, url, mime, status, digest, redirect, meta, offset,
    filename, record-id. Space separated. `-` means absent.
    """
    if not line or line.startswith(CDX_HEADER_PREFIX):
        return None
    parts = line.split(" ")
    if len(parts) < CDX_FIELDS:
        return None

    def clean(value: str) -> str | None:
        return None if value in ("-", "") else value

    status_raw = clean(parts[4])
    try:
        status = int(status_raw) if status_raw else None
    except ValueError:
        status = None

    return {
        "url": parts[0],
        "mime": clean(parts[3]),
        "status": status,
        "digest": clean(parts[5]),
        "redirect": clean(parts[6]),
        "offset": clean(parts[8]),
    }


def parse_log_error(line: str) -> tuple[str, str] | None:
    """Extract failures from wget's --no-verbose log.

    Errors arrive as two lines — the URL, then the reason:

        http://example.com/missing:
        2026-08-09 14:25:35 ERROR 404: Not Found.

    Only the reason line is recognised here; the caller supplies the URL it
    saw immediately before. Timeouts and refusals appear as `failed:` instead.
    """
    stripped = line.strip()
    if " ERROR " in stripped:
        return ("error", stripped.split(" ERROR ", 1)[1].strip())
    if stripped.endswith("failed: Connection refused.") or " failed: " in stripped:
        return ("error", stripped.split(" failed: ", 1)[-1].strip())
    if "unable to resolve host address" in stripped.lower():
        return ("error", stripped)
    return None


def build_argv(spec: JobSpec, out: Path, tmp: Path, job_dir: Path) -> list[str]:
    """Assemble the wget command line.

    Never build this as a shell string. URLs, hostnames, header values and
    regexes are all user-controlled and go straight into the argument list;
    `shell=False` with a list is what keeps them data instead of syntax.
    """
    cfg = spec.config
    scope = Scope.from_dict(spec.scope)
    ua = spec.auth.user_agent or str(cfg.get("user_agent", ""))

    argv = [
        WGET_BIN,
        # ── WARC output ──────────────────────────────────────────────────
        # No extension: wget appends the segment number and .warc.gz itself.
        "--warc-file",
        str(out / "warc" / "part"),
        "--warc-cdx",
        f"--warc-max-size={cfg.get('warc_max_size', '1G')}",
        # Must share a filesystem with the output or every segment close is a
        # full byte copy rather than a rename.
        "--warc-tempdir",
        str(tmp),
        "--warc-header",
        "operator: cairn",
        "--warc-header",
        f"isPartOf: {spec.site.slug}",
        "--warc-header",
        f"description: {spec.site.title}",
        # ── recursion ────────────────────────────────────────────────────
        "--recursive",
        "--page-requisites",
        # ── container hygiene ────────────────────────────────────────────
        # wget writes ~/.wget-hsts by default; $HOME may be read-only here.
        "--hsts-file",
        str(tmp / ".wget-hsts"),
        "--no-verbose",
        "--output-file",
        str(out / "crawl.log"),
    ]

    argv += to_wget_args(scope).args

    # ── politeness ───────────────────────────────────────────────────────
    argv += [
        f"--wait={cfg.get('wait_s', 1.0)}",
        f"--limit-rate={cfg.get('rate_limit', '2m')}",
        f"--tries={cfg.get('tries', 3)}",
        f"--timeout={cfg.get('timeout_s', 30)}",
        "--waitretry=10",
    ]
    if cfg.get("random_wait", True):
        argv.append("--random-wait")
    if cfg.get("content_on_error", True):
        argv.append("--content-on-error")

    # ── auth ─────────────────────────────────────────────────────────────
    if spec.auth.cookies_file:
        # Without --keep-session-cookies the interstitial cookie, which
        # frequently has no expiry, is dropped and the bypass silently fails.
        argv += ["--load-cookies", spec.auth.cookies_file, "--keep-session-cookies"]
    if ua:
        argv += ["--user-agent", ua]
    for key, value in spec.auth.headers.items():
        argv += ["--header", f"{key}: {value}"]

    # ── incremental ──────────────────────────────────────────────────────
    if spec.incremental.dedup_cdx and Path(spec.incremental.dedup_cdx).exists():
        argv += ["--warc-dedup", spec.incremental.dedup_cdx]

    # ── mirror ───────────────────────────────────────────────────────────
    #
    # wget is always given somewhere to write files, and `--delete-after` is
    # never used. That looks wasteful — the WARC already holds everything — but
    # the on-disk mirror is also how wget remembers what it has fetched, and
    # deleting each file immediately destroys that memory.
    #
    # It only shows with more than one seed. Measured on wget 1.25.0 against a
    # six-seed site whose ideal result is eight records:
    #
    #     --delete-after        38 records, 8 distinct   4.8x
    #     keep the files         8 records, 8 distinct   1.0x
    #
    # Every seed re-crawls the whole site. Handing the crawler a sitemap's
    # worth of URLs — the entire point of discovery — would therefore multiply
    # the work by the number of posts, hammering the origin and bloating the
    # archive, with nothing in the log to say so.
    #
    # When the user does not want the mirror it goes to the job's temp
    # directory and is removed with it, so the cost is transient disk during
    # the crawl rather than permanent storage.
    argv += [
        "--directory-prefix",
        str(out / "files") if cfg.get("keep_mirror") else str(tmp / "mirror"),
    ]

    # Seeds go in exactly one place. wget does NOT de-duplicate a URL that
    # appears both in --input-file and on the command line: it queues both and
    # crawls the whole site twice, doubling the time and the archive. (docs/05
    # showed both together; that is wrong and cost a full duplicate crawl in
    # the first end-to-end run.)
    seed_file = job_dir / (spec.seed_file or SEED_FILE)
    if seed_file.exists():
        argv += ["--input-file", str(seed_file)]
    else:
        argv += list(spec.seeds)
    return argv


def wget_version() -> str | None:
    try:
        proc = subprocess.run(  # noqa: S603
            [WGET_BIN, "--version"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return None
    first = proc.stdout.splitlines()[0] if proc.stdout else ""
    return first.strip() or None


class Runner:
    """Owns the wget process and the translation of its output into events."""

    def __init__(self, spec: JobSpec, events: EventWriter) -> None:
        self.spec = spec
        self.events = events
        self.out = Path(spec.output_dir)
        self.tmp = Path(spec.temp_dir)
        self.job_dir = Path(spec.temp_dir)
        self.proc: subprocess.Popen[bytes] | None = None
        self.terminating = False

        self.urls = 0
        self.errors = 0
        self.revisits = 0
        self.bytes = 0
        self._last_progress = 0.0
        self._pending_url: str | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    def request_stop(self, _signum: int, _frame: FrameType | None) -> None:
        """SIGTERM: let wget close and flush its WARC rather than killing it.

        Killing outright leaves a truncated final gzip member. wget's own
        termination path closes the current record first, so the archive stays
        readable and the result is reported as `partial` (docs/05).
        """
        if self.terminating:
            return
        self.terminating = True
        self.events.log("cancellation requested; asking wget to stop", level="warning")
        if self.proc and self.proc.poll() is None:
            with contextlib.suppress(OSError):
                self.proc.terminate()

    def run(self) -> int:
        (self.out / "warc").mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)

        argv = build_argv(self.spec, self.out, self.tmp, self.job_dir)
        self.events.started(tool_version=wget_version())
        self.events.log(f"wget {' '.join(shlex.quote(a) for a in argv[1:])}", level="debug")

        cdx = Tailer(self.out / "warc" / "part.cdx")
        crawl_log = Tailer(self.out / "crawl.log")

        started = time.monotonic()
        # cwd is the temp dir: with --delete-after wget still materializes
        # each file before removing it, and that must not happen inside the
        # archive tree where a crash would leave debris next to the WARCs.
        self.proc = subprocess.Popen(  # noqa: S603
            argv,
            cwd=str(self.tmp),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        while self.proc.poll() is None:
            self._drain(cdx, crawl_log, started)
            if self._over_limit(started):
                break
            time.sleep(POLL_INTERVAL_S)

        code = self._finish(cdx, crawl_log, started)
        return code

    def _over_limit(self, started: float) -> bool:
        limit = self.spec.limits.max_duration_s
        if limit and (time.monotonic() - started) > limit:
            self.events.warning("duration_limit", f"capture exceeded its {limit}s limit; stopping")
            self.request_stop(signal.SIGTERM, None)
            return True
        return False

    def _finish(self, cdx: Tailer, crawl_log: Tailer, started: float) -> int:
        assert self.proc is not None
        stderr = b""
        try:
            _, stderr = self.proc.communicate(timeout=DEFAULT_GRACE_S)
        except subprocess.TimeoutExpired:
            self.events.log("wget did not exit in time; killing it", level="warning")
            with contextlib.suppress(OSError):
                self.proc.kill()
            _, stderr = self.proc.communicate()

        # Drain whatever landed between the last poll and exit.
        self._drain(cdx, crawl_log, started, force_progress=True)

        if stderr:
            sys.stderr.write(stderr.decode("utf-8", errors="replace"))
            sys.stderr.flush()

        self._register_artifacts()

        code = self.proc.returncode
        stats = {
            "urls": self.urls,
            "errors": self.errors,
            "revisits": self.revisits,
            "bytes": self.bytes,
            "duration_s": round(time.monotonic() - started, 1),
            "wget_exit": code,
        }

        if self.terminating:
            self.events.result("partial", stats, error="cancelled")
            return 0
        if code == EXIT_OK:
            self.events.result("ok", stats)
            return 0
        if code in EXIT_PARTIAL and self.urls > 0:
            # Some URLs failed. With content on a real site that is the normal
            # outcome, not a failure — the gaps are listed and retryable.
            self.events.result("partial", stats, error=f"wget exited {code}")
            return 0
        if self.urls > 0:
            self.events.result("partial", stats, error=f"wget exited {code}")
            return 0
        self.events.result(
            "failed",
            stats,
            error=f"wget exited {code} without archiving anything.\n{self._log_tail()}",
        )
        return code or 1

    def _log_tail(self, lines: int = 15) -> str:
        """The last few lines of crawl.log, for the failure message.

        `--output-file` sends *everything* wget prints into that file, so its
        stderr is empty and a failure otherwise surfaces as a bare exit code.
        The one line that explains it — "Could not open temporary WARC
        manifest file", "Invalid regular expression" — is only in here.
        """
        path = self.out / "crawl.log"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(no crawl log was written)"
        tail = [line for line in text.splitlines() if line.strip()][-lines:]
        return "\n".join(tail) if tail else "(the crawl log is empty)"

    # ── output translation ───────────────────────────────────────────────

    def _drain(
        self, cdx: Tailer, crawl_log: Tailer, started: float, *, force_progress: bool = False
    ) -> None:
        for line in cdx.lines():
            record = parse_cdx_line(line)
            if record is None:
                continue
            self.urls += 1
            status = record["status"]
            if status is not None and status >= 400:
                self.errors += 1
            self.events.url(
                record["url"],
                status=status,
                mime=record["mime"],
                digest=record["digest"],
                redirect=record["redirect"],
            )

        for line in crawl_log.lines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.endswith(":") and "://" in stripped:
                # The URL line that precedes an error line.
                self._pending_url = stripped[:-1]
                continue
            failure = parse_log_error(stripped)
            if failure is not None:
                url = self._pending_url
                self._pending_url = None
                # A 4xx/5xx with a body is already counted from the CDX; only
                # count failures that produced no record at all.
                if url and "ERROR " not in stripped:
                    self.errors += 1
                    self.events.url(url, status=None, error=failure[1])
                elif url:
                    self.events.log(f"{url}: {failure[1]}", level="warning")
            elif stripped.startswith("Total wall clock") or stripped.startswith("FINISHED"):
                self.events.log(stripped, level="info")

        now = time.monotonic()
        if force_progress or (now - self._last_progress) >= PROGRESS_INTERVAL_S:
            self._last_progress = now
            self.bytes = _warc_bytes(self.out / "warc")
            elapsed = max(now - started, 0.001)
            self.events.progress(
                self.urls,
                bytes=self.bytes,
                rate_bps=round(self.bytes / elapsed, 1),
            )

    def _register_artifacts(self) -> None:
        warc_dir = self.out / "warc"
        if not warc_dir.is_dir():
            return
        for path in sorted(warc_dir.glob("*.warc.gz")):
            self.events.artifact(
                "warc",
                str(path.relative_to(self.out)).replace(os.sep, "/"),
                size=path.stat().st_size,
            )
        cdx = warc_dir / "part.cdx"
        if cdx.exists():
            # Kept for the NEXT run's --warc-dedup, not for replay (docs/00 D11).
            self.events.artifact(
                "cdx", str(cdx.relative_to(self.out)).replace(os.sep, "/"), size=cdx.stat().st_size
            )
        log_file = self.out / "crawl.log"
        if log_file.exists():
            self.events.artifact(
                "log",
                str(log_file.relative_to(self.out)).replace(os.sep, "/"),
                size=log_file.stat().st_size,
            )


def _warc_bytes(warc_dir: Path) -> int:
    total = 0
    if not warc_dir.is_dir():
        return 0
    for path in warc_dir.glob("*.warc.gz"):
        try:
            total += path.stat().st_size
        except OSError:  # pragma: no cover
            continue
    return total


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        sys.stderr.write("usage: python -m cairn.engines.wget <job.json>\n")
        return 2

    events = EventWriter()
    try:
        spec = JobSpec.load(args[0])
    except (OSError, ValueError) as exc:
        events.result("failed", {}, error=f"could not read job spec: {exc}")
        return 2

    runner = Runner(spec, events)
    signal.signal(signal.SIGTERM, runner.request_stop)
    signal.signal(signal.SIGINT, runner.request_stop)

    try:
        return runner.run()
    except Exception as exc:
        events.result("failed", {}, error=f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
