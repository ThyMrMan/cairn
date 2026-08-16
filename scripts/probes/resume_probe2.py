"""Does that saved state actually resume, or is it just a file?

Probe 1 proved browsertrix writes resumable state on SIGTERM — the signal
Cairn sends. That is only half the question. A state file that exists but
replays the whole crawl from the start would make "pause" a lie: the archive
would double and nothing would be saved.

So: take the state file probe 1 left behind, hand it back as `--config`, and
check what the second run actually fetches. The proof is *negative* — the
pages already finished must NOT be fetched again.

Run resume_probe.py first; this reads its output directory.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from pathlib import Path

from resume_probe import COLLECTION, IMAGE, Handler  # same fixture

HERE = Path(__file__).resolve().parent
ROOT = HERE / "resume-probe"
SOURCE_ARM = "sigterm-default"


def finished_from(state: Path) -> list[str]:
    """The URLs the interrupted run had already completed."""
    out, inside = [], False
    for line in state.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("finished:"):
            inside = True
            continue
        if inside:
            if line.strip().startswith("- "):
                out.append(line.strip()[2:].strip())
            elif line.strip() and not line.startswith(" " * 4):
                break
    return out


def main() -> None:
    work = ROOT / SOURCE_ARM
    states = sorted((work / "collections" / COLLECTION / "crawls").glob("*.yaml"))
    if not states:
        raise SystemExit(f"no state file under {work} — run resume_probe.py first")
    state = states[-1]
    done = finished_from(state)
    warcs_before = sorted((work / "collections" / COLLECTION / "archive").glob("*.warc.gz"))
    print(f"resuming from {state.name}")
    print(f"  already finished : {len(done)} pages")
    print(f"  warcs before     : {len(warcs_before)}\n")

    # The fixture has to come back on the SAME port: the queued URLs in the
    # state file are absolute and point at it.
    port = int(re.search(r"host\.docker\.internal:(\d+)", done[0]).group(1))
    try:
        server = __import__("http.server", fromlist=["ThreadingHTTPServer"]).ThreadingHTTPServer(
            ("0.0.0.0", port), Handler
        )
    except OSError as exc:
        raise SystemExit(f"cannot rebind :{port} — {exc}") from exc
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"fixture back on :{port}\n")

    container = "cairn-resume-probe-resumed"
    subprocess.run(["docker", "rm", "-f", container], capture_output=True)
    inside = f"/crawls/collections/{COLLECTION}/crawls/{state.name}"
    argv = [
        "docker",
        "run",
        "--rm",
        "--name",
        container,
        "--shm-size=2g",
        "--add-host=host.docker.internal:host-gateway",
        "-v",
        f"{work}:/crawls",
        IMAGE,
        "crawl",
        "--config",
        inside,
        # Reapplied deliberately: the docs say command-line options are not
        # persisted in the state file. Cairn rebuilds argv from the scope
        # anyway, so this costs nothing there.
        "--collection",
        COLLECTION,
        "--url",
        f"http://host.docker.internal:{port}/",
        "--scopeType",
        "prefix",
        "--workers",
        "1",
        "--generateCDX",
        "--limit",
        "12",
    ]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=420)
    server.shutdown()

    fetched = re.findall(r'"Starting page".*?"page":"([^"]+)"', proc.stdout + proc.stderr)
    if not fetched:  # log shape differs between versions; fall back to any URL mention
        fetched = re.findall(r'"page":"(http://host\.docker\.internal[^"]+)"', proc.stdout)
    refetched = [u for u in fetched if u in done]
    fresh = [u for u in fetched if u not in done]
    warcs_after = sorted((work / "collections" / COLLECTION / "archive").glob("*.warc.gz"))

    print("=" * 68)
    print(f"exit code            : {proc.returncode}")
    print(f"pages this run       : {len(fetched)}")
    verdict_note = "<-- RESUME IS A LIE" if refetched else "(none)"
    print(f"  already-done again : {len(refetched)}  {verdict_note}")
    print(f"  new pages          : {len(fresh)}")
    print(f"warcs before / after : {len(warcs_before)} / {len(warcs_after)}")
    print("=" * 68)
    if fresh:
        print("\nfirst few new pages:")
        for url in fresh[:5]:
            print(f"  {url}")
    if refetched:
        print("\nre-fetched despite being finished:")
        for url in refetched[:5]:
            print(f"  {url}")

    verdict = (
        "RESUMES — picked up the queue, did not repeat finished work"
        if fetched and not refetched
        else "DOES NOT RESUME — repeated work already done"
        if refetched
        else "INCONCLUSIVE — this run fetched nothing at all"
    )
    print(f"\n{verdict}")
    (ROOT / "resume-results.json").write_text(
        json.dumps(
            {
                "state_file": state.name,
                "finished_before": len(done),
                "fetched_this_run": fetched,
                "refetched": refetched,
                "warcs_before": len(warcs_before),
                "warcs_after": len(warcs_after),
                "exit_code": proc.returncode,
                "verdict": verdict,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
