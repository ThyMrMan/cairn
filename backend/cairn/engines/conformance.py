"""Does this engine actually honour the protocol? (docs/05)

The addon system is only real if somebody other than its author can use it,
and what makes that true is a test they can run before shipping. So this runs
a candidate engine against a fixture site it starts itself and checks every
rule core relies on — including the ones core enforces silently, which are
exactly the ones an addon author never discovers until a capture behaves
strangely six months later.

The rules, and why each is here rather than left to good intentions:

  - **A terminal `result` is mandatory.** Core treats its absence as failure
    whatever the exit code, because an engine that stopped without saying how
    it went cannot be distinguished from one that crashed.
  - **The exit code has to agree with it.** `result: ok` followed by exit 1
    does not get to be ok.
  - **Artifacts live inside `output_dir`.** Engine output is data, not
    instruction; a relative path with enough `..` in it would otherwise have
    core checksum, and later serve, a file anywhere on disk.
  - **stdout is NDJSON and nothing else.** A stray `print()` is survivable —
    core counts and skips malformed lines — but it is still a bug, so it is
    reported rather than ignored.
  - **`url` events are how a capture gets a URL list.** An engine that writes
    a perfectly good WARC and emits none produces a capture the UI cannot
    describe.

Run it with `cairn engines test <id-or-directory>`.
"""

from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from cairn.engines.protocol import (
    JOB_SPEC_FILE,
    PROTOCOL_VERSION,
    SEED_FILE,
    ArtifactEvent,
    ResultEvent,
    StartedEvent,
    UrlEvent,
    parse_event,
)
from cairn.engines.registry import Engine

DEFAULT_TIMEOUT_S = 900

FIXTURE_PAGES: dict[str, tuple[str, bytes]] = {
    "/": (
        "text/html",
        b"<html><body><h1>Conformance fixture</h1>"
        b"<a href='/page-2.html'>two</a><img src='/logo.png'></body></html>",
    ),
    "/page-2.html": ("text/html", b"<html><body><p>CONFORMANCE-MARKER</p></body></html>"),
    "/logo.png": ("image/png", b"\x89PNG\r\n\x1a\n" + b"LOGO" * 16),
    "/robots.txt": ("text/plain", b"User-agent: *\nAllow: /\n"),
}


@dataclass(slots=True)
class Check:
    name: str
    passed: bool
    detail: str = ""

    def __str__(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        return f"  [{mark}] {self.name}{f' — {self.detail}' if self.detail else ''}"


@dataclass(slots=True)
class Report:
    engine_id: str
    checks: list[Check] = field(default_factory=list)
    events: dict[str, int] = field(default_factory=dict)
    returncode: int = 0
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return all(check.passed for check in self.checks)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(Check(name=name, passed=passed, detail=detail))


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        hit = FIXTURE_PAGES.get(self.path.split("?")[0])
        ctype, body = hit or ("text/html", b"<html><body>not found</body></html>")
        self.send_response(200 if hit else 404)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


def _serve() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/"


def build_spec(engine: Engine, *, seed: str, output_dir: Path, temp_dir: Path) -> dict[str, Any]:
    """A job spec of exactly the shape core writes."""
    host = seed.split("//", 1)[1].split("/", 1)[0].split(":")[0]
    return {
        "protocol": PROTOCOL_VERSION,
        "job_id": 0,
        "job_type": "capture",
        "site": {"id": 0, "slug": "conformance", "title": "Conformance fixture"},
        "output_dir": str(output_dir),
        "temp_dir": str(temp_dir),
        "seeds": [seed],
        "seed_file": SEED_FILE,
        "scope": {
            "schema": 1,
            "seeds": [seed],
            "hosts": [
                {
                    "host": host,
                    "crawl_pages": True,
                    "fetch_assets": True,
                    "path_prefix": None,
                    "allow_extensionless": False,
                }
            ],
            "exclude_hosts": [],
            "accept_patterns": [],
            "reject_patterns": [],
            "max_depth": None,
            "max_pages": 25,
            "max_bytes": None,
            "obey_robots": True,
            "politeness": {"wait_s": 0, "random_wait": False, "rate_limit": "10m"},
        },
        "auth": {"user_agent": "cairn-conformance/1.0", "headers": {}},
        "incremental": {},
        "config": engine.defaults(),
        "limits": {},
    }


def run(
    engine: Engine,
    workdir: Path,
    *,
    seed: str | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> Report:
    """Run one engine against the fixture and judge what it emitted."""
    report = Report(engine_id=engine.id)
    server = None
    if seed is None:
        server, seed = _serve()

    output_dir = workdir / "out"
    temp_dir = workdir / "job"
    for path in (output_dir / "warc", temp_dir):
        path.mkdir(parents=True, exist_ok=True)

    spec = build_spec(engine, seed=seed, output_dir=output_dir, temp_dir=temp_dir)
    (temp_dir / JOB_SPEC_FILE).write_text(json.dumps(spec, indent=2), encoding="utf-8")
    (temp_dir / SEED_FILE).write_text(seed + "\n", encoding="utf-8")

    try:
        if engine.runtime.get("type") == "docker":
            report.add(
                "runs",
                False,
                "container engines cannot be exercised from here yet; validate the "
                "manifest and run the image by hand against a job.json",
            )
            return report
        proc = subprocess.run(  # noqa: S603 — running the engine is the point
            [*engine.command, str(temp_dir / JOB_SPEC_FILE)],
            capture_output=True,
            text=True,
            cwd=str(temp_dir),
            timeout=timeout_s,
            env=_env(engine),
        )
    except FileNotFoundError as exc:
        report.add("runs", False, f"could not start it: {exc}")
        return report
    except subprocess.TimeoutExpired:
        report.add("runs", False, f"it did not finish within {timeout_s}s")
        return report
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()

    report.returncode = proc.returncode
    report.stderr = proc.stderr[-4000:]
    _judge(report, proc.stdout, output_dir)
    return report


def _env(engine: Engine) -> dict[str, str]:
    import os

    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.update(engine.env_overrides())
    return env


def _judge(report: Report, stdout: str, output_dir: Path) -> None:
    events: list[Any] = []
    malformed: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parsed = parse_event(line)
        if parsed is None:
            malformed.append(line)
            continue
        events.append(parsed)
        name = type(parsed).__name__.replace("Event", "").lower()
        report.events[name] = report.events.get(name, 0) + 1

    report.add("runs", True)
    report.add(
        "stdout is NDJSON",
        not malformed,
        "" if not malformed else f"{len(malformed)} unparseable line(s): {malformed[0][:120]!r}",
    )
    report.add(
        "emits a started event",
        any(isinstance(e, StartedEvent) for e in events),
        "core shows it as the first line of the live log",
    )

    urls = [e for e in events if isinstance(e, UrlEvent)]
    report.add(
        "emits url events",
        bool(urls),
        "" if urls else "without these the capture has no URL list and no error report",
    )

    results = [e for e in events if isinstance(e, ResultEvent)]
    report.add(
        "emits exactly one result",
        len(results) == 1,
        f"saw {len(results)}; core treats none as a failure whatever the exit code",
    )

    if results:
        status = results[-1].status
        agrees = (report.returncode == 0) == (status in ("ok", "partial"))
        report.add(
            "the exit code agrees with the result",
            agrees,
            f"result {status!r} with exit {report.returncode}",
        )

    artifacts = [e for e in events if isinstance(e, ArtifactEvent)]
    report.add("declares its artifacts", bool(artifacts), "so they can be checksummed and served")

    # Only asked when there are artifacts. A "no violations" check over an
    # empty list passes, and a PASS that means "there was nothing to check"
    # reads exactly like a PASS that means "this is correct".
    if artifacts:
        escaping = [a.path for a in artifacts if not _inside(output_dir, a.path)]
        report.add(
            "artifact paths stay inside output_dir",
            not escaping,
            "" if not escaping else f"escapes: {escaping[:3]}",
        )
        missing = [a.path for a in artifacts if not (output_dir / a.path).is_file()]
        report.add(
            "declared artifacts are on disk",
            not missing,
            "" if not missing else f"missing: {missing[:3]}",
        )

    warcs = sorted(output_dir.rglob("*.warc.gz"))
    report.add(
        "wrote at least one WARC",
        bool(warcs),
        "" if warcs else "an engine with `warc` in its outputs must produce one",
    )


def _inside(root: Path, candidate: str) -> bool:
    from cairn.services.storage import StoragePathError, resolve_within

    try:
        resolve_within(root, candidate)
    except StoragePathError:
        return False
    return True
