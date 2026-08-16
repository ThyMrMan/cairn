"""Does browsertrix-crawler save resumable state on the signal Cairn sends?

The question, precisely. Cairn stops an engine container with Docker's stop,
which is SIGTERM then SIGKILL. browsertrix's docs say state is written when a
crawl is "interrupted" without naming a signal. If it only handles SIGINT then
every pause/resume feature built on top of this is dead on arrival — and it
would fail *silently*, as an empty directory rather than an error.

Three arms:

  1. SIGTERM, default --saveState (which is "partial")   ← the one that decides it
  2. SIGINT,  default --saveState                        ← the comparison
  3. SIGTERM, --saveState always --saveStateInterval 5   ← the fallback, and
     also what a pause would need to survive a crash rather than a clean stop

**The negative control matters more than the result here.** A run that reached
zero pages would produce an empty crawls/ directory for a reason that has
nothing to do with signals, and would look exactly like "SIGTERM does not
work". So every arm asserts it crawled real pages first, and refuses to draw a
conclusion otherwise. Earlier in this project a fixture the container could not
reach nearly proved the opposite of the truth twice.
"""

from __future__ import annotations

import http.server
import json
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

IMAGE = "webrecorder/browsertrix-crawler:1.14.1"
HERE = Path(__file__).resolve().parent
ROOT = HERE / "resume-probe"
COLLECTION = "capture"  # same name Cairn uses
PAGES = 60
PAGE_DELAY_S = 0.25  # slow enough that the crawl is still running when we hit it
INTERRUPT_AFTER_PAGES = 6


# ── a small site the crawl cannot finish instantly ───────────────────────


def _page(n: int) -> bytes:
    links = "".join(f'<li><a href="/p{m}.html">post {m}</a></li>' for m in range(PAGES) if m != n)
    return (
        f"<!doctype html><html><head><title>Post {n}</title></head>"
        f"<body><h1>Post {n}</h1><p>Body of post {n}.</p><ul>{links}</ul></body></html>"
    ).encode()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        time.sleep(PAGE_DELAY_S)
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            body = _page(0)
        elif path.startswith("/p") and path.endswith(".html"):
            try:
                body = _page(int(path[2:-5]))
            except ValueError:
                self.send_error(404)
                return
        elif path == "/robots.txt":
            body = b"User-agent: *\nAllow: /\n"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        return int(s.getsockname()[1])


# ── one arm ──────────────────────────────────────────────────────────────


def run_arm(name: str, port: int, *, signal: str, extra: list[str]) -> dict[str, object]:
    work = ROOT / name
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    container = f"cairn-resume-probe-{name}"
    subprocess.run(["docker", "rm", "-f", container], capture_output=True)

    argv = [
        "docker",
        "run",
        "-d",
        "--name",
        container,
        "--shm-size=2g",
        "--add-host=host.docker.internal:host-gateway",
        "-v",
        f"{work}:/crawls",
        IMAGE,
        "crawl",
        "--collection",
        COLLECTION,
        "--url",
        f"http://host.docker.internal:{port}/",
        "--scopeType",
        "prefix",
        "--workers",
        "1",
        "--generateCDX",
        "--pageExtraDelay",
        "1",
        "--limit",
        str(PAGES),
        *extra,
    ]
    started = subprocess.run(argv, capture_output=True, text=True)
    if started.returncode != 0:
        return {"arm": name, "error": started.stderr.strip()[:400]}

    # Wait until it is genuinely mid-crawl. This doubles as the reachability
    # check: a container that cannot see the fixture never gets here.
    pages = 0
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        logs = subprocess.run(["docker", "logs", container], capture_output=True, text=True).stdout
        pages = logs.count('"Starting page"')
        if pages >= INTERRUPT_AFTER_PAGES:
            break
        if (
            subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", container],
                capture_output=True,
                text=True,
            ).stdout.strip()
            != "true"
        ):
            break  # it exited on its own; report what it managed
        time.sleep(1)

    # The interrupt under test.
    if signal == "SIGTERM":
        # Exactly what cairn.services.containers.stop does: Docker's stop with
        # a grace period, which is SIGTERM followed by SIGKILL.
        subprocess.run(["docker", "stop", "-t", "60", container], capture_output=True)
    else:
        subprocess.run(["docker", "kill", "--signal", signal, container], capture_output=True)
        for _ in range(60):
            alive = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", container],
                capture_output=True,
                text=True,
            ).stdout.strip()
            if alive != "true":
                break
            time.sleep(1)

    code = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.ExitCode}}", container],
        capture_output=True,
        text=True,
    ).stdout.strip()
    logs = subprocess.run(["docker", "logs", container], capture_output=True, text=True).stdout
    subprocess.run(["docker", "rm", "-f", container], capture_output=True)

    collection = work / "collections" / COLLECTION
    state_dir = collection / "crawls"
    archive = collection / "archive"
    states = sorted(state_dir.glob("*.yaml")) if state_dir.is_dir() else []
    warcs = sorted(archive.glob("*.warc.gz")) if archive.is_dir() else []

    result: dict[str, object] = {
        "arm": name,
        "signal": signal,
        "pages_started": logs.count('"Starting page"'),
        "exit_code": code,
        "warcs": len(warcs),
        "warc_bytes": sum(w.stat().st_size for w in warcs),
        "state_files": [s.name for s in states],
    }
    if states:
        text = states[-1].read_text(encoding="utf-8", errors="replace")
        result["state_bytes"] = len(text)
        # A state file with no pending queue would be a file, not a resume.
        result["mentions_queue"] = any(k in text for k in ("queued", "pending", "seen"))
        result["state_head"] = "\n".join(text.splitlines()[:14])
    return result


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    port = free_port()
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"fixture on :{port}, {PAGES} pages, {PAGE_DELAY_S}s per response\n")

    arms = [
        ("sigterm-default", "SIGTERM", []),
        ("sigint-default", "SIGINT", []),
        ("sigterm-always", "SIGTERM", ["--saveState", "always", "--saveStateInterval", "5"]),
    ]
    results = []
    for name, sig, extra in arms:
        print(f"── {name} ({sig}{' ' + ' '.join(extra) if extra else ''})")
        r = run_arm(name, port, signal=sig, extra=extra)
        results.append(r)
        if r.get("error"):
            print(f"   docker refused: {r['error']}\n")
            continue
        print(f"   pages started : {r['pages_started']}")
        print(f"   exit code     : {r['exit_code']}")
        print(f"   warcs         : {r['warcs']} ({r['warc_bytes']} bytes)")
        print(f"   state files   : {r['state_files'] or 'NONE'}")
        if r.get("state_files"):
            print(f"   state bytes   : {r['state_bytes']}, queue present: {r['mentions_queue']}")
        print()

    server.shutdown()

    print("=" * 68)
    for r in results:
        if r.get("error"):
            print(f"{r['arm']:18} INCONCLUSIVE — docker refused to start it")
            continue
        if not r["pages_started"]:
            print(f"{r['arm']:18} INCONCLUSIVE — crawled 0 pages, so an empty crawls/")
            print(f"{'':18} says nothing about signals. Check reachability first.")
            continue
        verdict = "SAVES STATE" if r["state_files"] else "NO STATE WRITTEN"
        print(f"{r['arm']:18} {verdict}  ({r['pages_started']} pages crawled first)")
    print("=" * 68)

    head = next((r for r in results if r.get("state_head")), None)
    if head:
        print(f"\nstate file from {head['arm']}, first lines:\n")
        print(head["state_head"])

    (ROOT / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nfull results: {ROOT / 'results.json'}")


if __name__ == "__main__":
    main()
