#!/usr/bin/env python3
"""A cairn capture engine, in as few moving parts as the protocol allows.

Fetches each seed once and writes a WARC. That is all — no recursion, no page
requisites, no scope enforcement beyond the host list. Everything else here is
the protocol, which is the part worth copying.

    cairn engines validate .    # is the manifest well formed?
    cairn engines test .        # does this honour the protocol?

The contract, in full:

  1. You are run with the path to `job.json` as the last argument.
  2. You write one JSON object per line to **stdout**, and nothing else.
     Diagnostics go to stderr; cairn keeps the tail of it for failures.
  3. You write your output under `output_dir` and declare each file with an
     `artifact` event, using a path *relative to `output_dir`*. Cairn refuses
     any path that escapes it.
  4. You finish with exactly one `result` event, and your exit code has to
     agree with it. No `result` means failure whatever the exit code, because
     an engine that stopped without saying how it went is indistinguishable
     from one that crashed.
  5. On SIGTERM you stop, flush whatever you have, and report `partial`. A
     cancelled capture should cost a partial archive, not a truncated one.

Needs `warcio`. An engine brings its own dependencies; it never imports cairn.
"""

from __future__ import annotations

import json
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

stopping = False


def emit(**fields: Any) -> None:
    """One protocol event. Flushed, always.

    Without the flush these sit in a pipe buffer and the live log in the UI
    stays empty until the crawl ends — which is exactly when nobody needs it.
    """
    sys.stdout.write(json.dumps(fields, separators=(",", ":"), default=str) + "\n")
    sys.stdout.flush()


def on_term(_signum: int, _frame: Any) -> None:
    global stopping
    stopping = True
    emit(type="log", level="warning", msg="stopping on request")


def fetch(url: str, *, timeout: int, agent: str, cookies: str | None) -> tuple[int, dict, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": agent})  # noqa: S310
    if cookies:
        request.add_header("Cookie", cookies)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.status, dict(response.headers), response.read()


def cookie_header(path: str | None) -> str | None:
    """Netscape cookies.txt → one `Cookie:` header.

    Cairn hands you a file rather than a header because that is the format
    people export, and because the file is deleted when the job ends.
    """
    if not path or not Path(path).is_file():
        return None
    pairs = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) >= 7:
            pairs.append(f"{fields[5]}={fields[6]}")
    return "; ".join(pairs) or None


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: engine.py <job.json>", file=sys.stderr)
        return 2

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    spec = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    output = Path(spec["output_dir"])
    config = spec.get("config") or {}
    auth = spec.get("auth") or {}
    timeout = int(config.get("timeout_s", 30))
    agent = auth.get("user_agent") or str(config.get("user_agent") or "cairn-engine-template")
    cookies = cookie_header(auth.get("cookies_file"))

    # Seeds arrive twice over: inline, and in a file. The file is authoritative
    # for a large crawl — a sitemap's worth of URLs does not belong on a
    # command line — but either is fine to read.
    seeds: list[str] = list(spec.get("seeds") or [])
    seed_file = Path(spec["temp_dir"]) / (spec.get("seed_file") or "seeds.txt")
    if not seeds and seed_file.is_file():
        seeds = [s.strip() for s in seed_file.read_text().splitlines() if s.strip()]

    emit(type="started", tool_version="engine-template/1.0")

    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    warc_dir = output / "warc"
    warc_dir.mkdir(parents=True, exist_ok=True)
    warc_path = warc_dir / "part-00000.warc.gz"

    started = time.monotonic()
    fetched = 0
    errors = 0

    with warc_path.open("wb") as handle:
        writer = WARCWriter(handle, gzip=True)
        for url in seeds:
            if stopping:
                break
            try:
                status, headers, body = fetch(url, timeout=timeout, agent=agent, cookies=cookies)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                errors += 1
                # A URL event with an error is how a failure reaches the UI's
                # gap report. Do not just log it.
                emit(type="url", url=url, error=str(exc)[:300])
                continue

            record = writer.create_warc_record(
                url,
                "response",
                payload=_BytesIO(body),
                http_headers=StatusAndHeaders(
                    f"{status} OK", list(headers.items()), protocol="HTTP/1.1"
                ),
            )
            writer.write_record(record)
            fetched += 1
            emit(
                type="url",
                url=url,
                status=status,
                mime=headers.get("Content-Type", "").split(";")[0] or None,
                size=len(body),
            )
            emit(type="progress", done=fetched, total=len(seeds), bytes=warc_path.stat().st_size)

    emit(
        type="artifact",
        kind="warc",
        # Relative to output_dir. An absolute path, or one with enough `..` in
        # it, is refused — engine output is data, not instruction.
        path=f"warc/{warc_path.name}",
        size=warc_path.stat().st_size,
    )

    stats = {
        "urls": fetched,
        "errors": errors,
        "bytes": warc_path.stat().st_size,
        "duration_s": round(time.monotonic() - started, 1),
    }
    if stopping:
        emit(type="result", status="partial", stats=stats, error="cancelled")
    elif fetched and errors:
        # `partial` is a first-class success: an archive with known gaps is
        # not a failed capture.
        emit(type="result", status="partial", stats=stats, error=f"{errors} URL(s) failed")
    elif fetched:
        emit(type="result", status="ok", stats=stats)
    else:
        emit(type="result", status="failed", stats=stats, error="nothing was archived")
        return 1
    return 0


class _BytesIO:
    """warcio wants a file-like payload; this keeps the example dependency-free."""

    def __init__(self, data: bytes) -> None:
        from io import BytesIO

        self._inner = BytesIO(data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
