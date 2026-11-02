"""The `wayback-import` engine: `python -m cairn.engines.wayback <job.json>`.

Copies archived snapshots from the Wayback Machine into WARCs of our own
(docs/17 phase 3, docs/19 stage 2). Its input is the survey's selection: one
line per URL with the chosen snapshot and a few older fallbacks.

What it relies on, each measured rather than assumed (docs/17):

- `web/<ts>id_/<url>` returns the archived bytes verbatim, still compressed
  if they were, with the original headers as `X-Archive-Orig-*` and the real
  snapshot time as `Memento-Datetime`. The SHA-1 of the raw body is the CDX
  digest. So bodies are read raw and stored untouched.
- A response with neither `Memento-Datetime` nor any `X-Archive-Orig-*` is
  archive.org talking, not the archive, and is never stored.
- An archived redirect comes back as the redirect, with `Memento-Datetime`.
  It is stored as one. A redirect *without* it is Wayback moving us to a
  nearby timestamp, and is followed.

Resumable by a ledger, `wayback-ledger.jsonl` in the capture directory: one
line per URL settled. A pause, a crash, a tripped breaker or a spent daily
budget all continue from it, into the same capture. The engine pauses itself
for the last two (`self_paused`, `resume_after_s`); core turns that into a
paused capture and resumes it when the time comes.
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import hashlib
import http
import io
import json
import os
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import FrameType
from typing import Any, BinaryIO

import httpx
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from cairn import __version__
from cairn.engines.protocol import RESUME_STATE_FILE, EventWriter, JobSpec, ResultStatus
from cairn.net.polite import (
    Pacing,
    PoliteClient,
    PoliteError,
    RateLimitedError,
    StopRequestedError,
)

LEDGER_FILE = "wayback-ledger.jsonl"
CDX_FILE = "part.cdx"
CDX_HEADER = " CDX a b a m s k r M V g u"
WAYBACK = "https://web.archive.org/web"
# Tests point the engine at a fake archive.org on localhost. An environment
# variable rather than config, so nothing a site stores can redirect an import.
BASE_ENV = "CAIRN_WAYBACK_BASE"
RECORDS_PER_PART = 1000
# Wayback's own hops to a nearby timestamp, before giving up on a candidate.
MAX_WAYBACK_HOPS = 3
# Failures (not "gone") before a URL is left alone for this capture.
MAX_FAILURES = 3
DAY_S = 24 * 3600
HOP_BY_HOP = {"transfer-encoding", "connection", "keep-alive", "content-length"}

DONE, GONE, FAILED = "done", "gone", "failed"


def sha1_base32(body: bytes) -> str:
    return base64.b32encode(hashlib.sha1(body).digest()).decode("ascii")  # noqa: S324 — CDX digest


@dataclass(slots=True)
class Entry:
    key: str
    url: str
    kind: str
    mime: str
    candidates: list[tuple[str, str]]  # (timestamp, digest), newest first

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Entry:
        older = [(str(ts), str(d)) for ts, d in raw.get("older") or []]
        return cls(
            key=str(raw["key"]),
            url=str(raw["url"]),
            kind=str(raw.get("kind") or "page"),
            mime=str(raw.get("mime") or ""),
            candidates=[(str(raw["ts"]), str(raw.get("digest") or "")), *older],
        )


def read_selection(path: Path) -> list[Entry]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [Entry.from_json(json.loads(line)) for line in fh if line.strip()]


# ── the ledger ───────────────────────────────────────────────────────────


@dataclass(slots=True)
class Ledger:
    path: Path
    states: dict[str, str] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    # (epoch seconds, requests) per settled URL, for the daily budget.
    spend: list[tuple[float, int]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> Ledger:
        ledger = cls(path)
        with contextlib.suppress(FileNotFoundError):
            for line in path.read_text("utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # a torn last line from a crash
                ledger._apply(row)
        return ledger

    def _apply(self, row: dict[str, Any]) -> None:
        key, state = str(row.get("key")), str(row.get("state"))
        self.states[key] = state
        if state == FAILED:
            self.failures[key] = self.failures.get(key, 0) + 1
        self.spend.append((float(row.get("at") or 0), int(row.get("requests") or 0)))

    def settled(self, key: str) -> bool:
        state = self.states.get(key)
        return state in (DONE, GONE) or self.failures.get(key, 0) >= MAX_FAILURES

    def spent_since(self, since: float) -> int:
        return sum(n for at, n in self.spend if at >= since)

    def oldest_spend_after(self, since: float) -> float | None:
        times = [at for at, n in self.spend if at >= since and n]
        return min(times) if times else None

    def record(self, fh: BinaryIO, row: dict[str, Any]) -> None:
        fh.write((json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8"))
        fh.flush()
        self._apply(row)

    @property
    def counts(self) -> dict[str, int]:
        out = {DONE: 0, GONE: 0, FAILED: 0}
        for key, state in self.states.items():
            if state in (DONE, GONE):
                out[state] += 1
            elif self.failures.get(key, 0) >= MAX_FAILURES:
                out[FAILED] += 1
        return out


# ── WARC output ──────────────────────────────────────────────────────────


class WarcParts:
    """Rolling `.warc.gz` parts plus one `part.cdx` across every run."""

    def __init__(self, warc_dir: Path, stamp: str) -> None:
        self.dir = warc_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.stamp = stamp
        self.part = -1
        self.records = 0
        self._in_part = 0
        self._fh: BinaryIO | None = None
        self._writer: WARCWriter | None = None
        cdx = self.dir / CDX_FILE
        new = not cdx.exists()
        self._cdx = cdx.open("a", encoding="utf-8")
        if new:
            self._cdx.write(CDX_HEADER + "\n")
            self._cdx.flush()

    @property
    def name(self) -> str:
        return f"wayback-{self.stamp}-{self.part:05d}.warc.gz"

    def _roll(self) -> None:
        self.close_part()
        self.part += 1
        # Never reuse a name: two runs can start within the same second, and
        # opening an existing part for writing would truncate archived records.
        while (self.dir / self.name).exists():
            self.part += 1
        self._fh = (self.dir / self.name).open("xb")
        self._writer = WARCWriter(self._fh, gzip=True)
        self._in_part = 0
        info = {
            "software": f"Cairn/{__version__} wayback-import",
            "source": "Internet Archive Wayback Machine (https://web.archive.org/)",
            "format": "WARC File Format 1.1",
        }
        self._writer.write_record(self._writer.create_warcinfo_record(self.name, info))

    def write(
        self,
        *,
        url: str,
        status: int,
        headers: list[tuple[str, str]],
        body: bytes,
        snapshot: datetime,
        source: str,
        timestamp: str,
        mime: str,
        digest: str,
    ) -> None:
        if self._writer is None or self._in_part >= RECORDS_PER_PART:
            self._roll()
        assert self._writer is not None and self._fh is not None
        try:
            phrase = http.HTTPStatus(status).phrase
        except ValueError:
            phrase = "Unknown"
        http_headers = StatusAndHeaders(f"{status} {phrase}", headers, protocol="HTTP/1.1")
        record_id = self._writer._make_warc_id()
        record = self._writer.create_warc_record(
            url,
            "response",
            payload=io.BytesIO(body),
            length=len(body),
            http_headers=http_headers,
            warc_headers_dict={
                "WARC-Record-ID": record_id,
                "WARC-Date": snapshot.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "WARC-Source-URI": source,
                "WARC-Creation-Date": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        offset = self._fh.tell()
        self._writer.write_record(record)
        self.records += 1
        self._in_part += 1
        self._cdx.write(
            f"{url} {timestamp} {url} {mime or '-'} {status} {digest} - - "
            f"{offset} {self.name} {record_id}\n"
        )
        self._cdx.flush()

    def close_part(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self._writer = None

    def close(self) -> None:
        self.close_part()
        self._cdx.close()


# ── the run ──────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Fetched:
    state: str
    status: int | None = None
    size: int = 0
    mime: str | None = None
    digest_ok: bool | None = None
    timestamp: str | None = None
    error: str | None = None


class Runner:
    def __init__(
        self,
        spec: JobSpec,
        events: EventWriter,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.spec = spec
        self.events = events
        self.out = Path(spec.output_dir)
        self.tmp = Path(spec.temp_dir)
        self.config = dict(spec.config or {})
        self.stopping = False
        self.wall = wall
        rate = float(self.config.get("rate_per_min") or 10)
        self.client = PoliteClient(
            Pacing.per_minute(
                rate, max_consecutive_429=int(self.config.get("max_consecutive_429") or 5)
            ),
            user_agent=str(self.config.get("user_agent") or spec.auth.user_agent or "")
            or f"Cairn/{__version__} wayback-import",
            should_stop=lambda: self.stopping,
            on_wait=lambda why, _s: self.events.log(f"archive.org: {why}", level="warning"),
            transport=transport,
            sleep=sleep,
            clock=clock,
        )
        self.bad_digests = {str(d) for d in self.config.get("bad_digests") or []}
        self.base = (os.environ.get(BASE_ENV) or WAYBACK).rstrip("/")
        self.base_host = httpx.URL(self.base).host

    def request_stop(self, _signum: int, _frame: FrameType | None) -> None:
        self.stopping = True

    def run(self) -> int:
        self.events.started(tool_version=f"wayback-import {__version__}")
        selection_file = Path(str(self.config.get("selection_file") or ""))
        if not selection_file.is_file():
            self.events.result("failed", {}, error="no survey selection to import from")
            return 1
        entries = read_selection(selection_file)
        ledger = Ledger.load(self.out / LEDGER_FILE)
        pending = [e for e in entries if not ledger.settled(e.key)]
        total = len(entries)
        already = total - len(pending)
        self.events.log(
            f"{total:,} URLs selected; {already:,} already settled"
            + (" by an earlier run" if already else "")
            + f"; {len(pending):,} to fetch at about {self.config.get('rate_per_min', 10)}/min."
        )

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        parts = WarcParts(self.out / "warc", stamp)
        budget = int(self.config.get("daily_budget") or 0)
        cooldown_s = float(self.config.get("cooldown_min") or 60) * 60
        stop_reason: str | None = None
        resume_after: float | None = None
        fetched = errors = 0
        started = time.monotonic()
        max_s = float(self.spec.limits.max_duration_s or 0)

        with (self.out / LEDGER_FILE).open("ab") as ledger_fh:
            try:
                for index, entry in enumerate(pending, start=1):
                    if self.stopping:
                        stop_reason = "stopped"
                        break
                    if max_s and time.monotonic() - started >= max_s:
                        # The capture duration cap. An import outlasts it by
                        # design, so it ends a segment rather than the import:
                        # what is fetched gets indexed, and it carries on.
                        stop_reason = "segment"
                        resume_after = 60.0
                        break
                    if budget:
                        since = self.wall() - DAY_S
                        if ledger.spent_since(since) >= budget:
                            oldest = ledger.oldest_spend_after(since) or self.wall()
                            resume_after = max(oldest + DAY_S - self.wall(), 60.0)
                            stop_reason = "budget"
                            break
                    before = self.client.requests
                    result = self._fetch(entry, parts)
                    ledger.record(
                        ledger_fh,
                        {
                            "key": entry.key,
                            "url": entry.url,
                            "state": result.state,
                            "ts": result.timestamp,
                            "at": round(self.wall(), 1),
                            "requests": self.client.requests - before,
                            **({"digest_ok": False} if result.digest_ok is False else {}),
                            **({"error": result.error} if result.error else {}),
                        },
                    )
                    if result.state == DONE:
                        fetched += 1
                    elif result.state == FAILED:
                        errors += 1
                    self.events.url(
                        entry.url,
                        status=result.status,
                        mime=result.mime,
                        size=result.size or None,
                        error=None if result.state == DONE else (result.error or result.state),
                    )
                    self.events.progress(
                        already + index, total=total, bytes=_warc_bytes(self.out / "warc")
                    )
            except RateLimitedError as exc:
                stop_reason = "rate_limited"
                resume_after = cooldown_s
                self.events.warning("rate_limited", str(exc))
            except StopRequestedError:
                stop_reason = "stopped"
            finally:
                parts.close()

        counts = ledger.counts
        remaining = sum(1 for e in entries if not ledger.settled(e.key))
        stats: dict[str, Any] = {
            "urls": fetched,
            "errors": errors,
            "bytes": _warc_bytes(self.out / "warc"),
            "requests": self.client.requests,
            "duration_s": round(time.monotonic() - started, 1),
            "imported": counts[DONE],
            "gone": counts[GONE],
            "failed": counts[FAILED],
            "remaining": remaining,
        }
        self._write_state(stats, stop_reason)
        self._register_artifacts()

        if stop_reason in ("budget", "rate_limited", "segment"):
            stats.update(resumable=True, self_paused=True, resume_after_s=int(resume_after or 3600))
            why = {
                "budget": "the daily request budget is spent",
                "rate_limited": "archive.org kept answering 429",
                "segment": "this run reached the capture time limit",
            }[stop_reason]
            self.events.log(
                f"Pausing: {why}. {remaining:,} URLs left; resuming in about "
                f"{int((resume_after or 3600) // 60)} minutes.",
                level="warning",
            )
            self.events.result("partial", stats, error=f"paused: {why}")
            return 0
        if stop_reason == "stopped":
            stats["resumable"] = True
            self.events.result("partial", stats, error="stopped")
            return 0
        if counts[DONE] == 0 and total > 0:
            self.events.result("failed", stats, error="nothing could be fetched from Wayback")
            return 1
        status: ResultStatus = "ok" if counts[GONE] == 0 and counts[FAILED] == 0 else "partial"
        self.events.result(status, stats)
        return 0

    # ── one URL ──────────────────────────────────────────────────────────

    def _fetch(self, entry: Entry, parts: WarcParts) -> Fetched:
        last = Fetched(state=GONE, error="no usable snapshot")
        for timestamp, digest in entry.candidates:
            result = self._fetch_snapshot(entry, timestamp, digest, parts)
            if result.state == DONE:
                return result
            last = result
            if result.state == FAILED:
                # Transient: retry this URL on a later run rather than walk
                # back through older copies because archive.org had a moment.
                return result
        return last

    def _fetch_snapshot(
        self, entry: Entry, timestamp: str, digest: str, parts: WarcParts
    ) -> Fetched:
        source = f"{self.base}/{timestamp}id_/{entry.url}"
        target = source
        for _hop in range(MAX_WAYBACK_HOPS + 1):
            try:
                response = self.client.get(
                    target,
                    raw=True,
                    follow_redirects=False,
                    accept_status=lambda s: s < 500 and s != 429,
                )
            except PoliteError as exc:
                if isinstance(exc, RateLimitedError):
                    raise
                return Fetched(state=FAILED, error=str(exc))

            archived = _is_archived(response.headers)
            if not archived and 300 <= response.status < 400:
                location = response.headers.get("location", "")
                hop = httpx.URL(target).join(location)
                if hop.host != self.base_host:
                    return Fetched(state=GONE, error=f"Wayback redirected off-site: {hop}")
                target = str(hop)
                continue
            if not archived:
                if response.status in (403, 404, 410):
                    return Fetched(state=GONE, status=response.status, error="not in Wayback")
                # archive.org's own page, not the archive: never stored.
                return Fetched(state=FAILED, error=f"HTTP {response.status} without a snapshot")
            break
        else:
            return Fetched(state=GONE, error="too many Wayback redirects")

        body = response.body
        body_digest = sha1_base32(body)
        if body_digest in self.bad_digests:
            return Fetched(state=GONE, error="snapshot is a known error page")
        snapshot = _memento_time(response.headers) or _ts_time(timestamp)
        served_ts = snapshot.strftime("%Y%m%d%H%M%S")
        headers = _original_headers(response.headers, len(body))
        mime = (response.headers.get("content-type") or entry.mime or "").split(";")[0].strip()
        parts.write(
            url=entry.url,
            status=response.status,
            headers=headers,
            body=body,
            snapshot=snapshot,
            source=str(response.url),
            timestamp=served_ts,
            mime=mime,
            digest=body_digest,
        )
        return Fetched(
            state=DONE,
            status=response.status,
            size=len(body),
            mime=mime or None,
            digest_ok=_digest_matches(digest, body_digest, body, response.headers)
            if digest
            else None,
            timestamp=served_ts,
        )

    # ── bookkeeping ──────────────────────────────────────────────────────

    def _write_state(self, stats: dict[str, Any], reason: str | None) -> None:
        """The resume state core looks for. The ledger is the real state."""
        state = {
            "engine": "wayback-import",
            "ledger": LEDGER_FILE,
            "stopped": reason,
            "remaining": stats["remaining"],
            "written_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        path = self.out / RESUME_STATE_FILE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)

    def _register_artifacts(self) -> None:
        warc_dir = self.out / "warc"
        for path in sorted(warc_dir.glob("*.warc.gz")):
            self.events.artifact("warc", _rel(path, self.out), size=path.stat().st_size)
        cdx = warc_dir / CDX_FILE
        if cdx.exists():
            self.events.artifact("cdx", _rel(cdx, self.out), size=cdx.stat().st_size)
        ledger = self.out / LEDGER_FILE
        if ledger.exists():
            self.events.artifact("log", _rel(ledger, self.out), size=ledger.stat().st_size)


def _rel(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace(os.sep, "/")


def _digest_matches(want: str, raw_digest: str, body: bytes, headers: httpx.Headers) -> bool:
    """Whether the body is the snapshot the listing described.

    Measured on fictionmania (2026-11-01): newer copies match on the raw
    bytes, but copies archived uncompressed (2017) are gzipped by archive.org
    on the way out, and match only once decoded. Both are the right content.
    """
    if raw_digest == want:
        return True
    if "gzip" in (headers.get("content-encoding") or "").lower():
        with contextlib.suppress(OSError, EOFError, ValueError):
            return sha1_base32(gzip.decompress(body)) == want
    return False


def _is_archived(headers: httpx.Headers) -> bool:
    return "memento-datetime" in headers or any(
        name.lower().startswith("x-archive-orig-") for name in headers
    )


def _memento_time(headers: httpx.Headers) -> datetime | None:
    value = headers.get("memento-datetime")
    if not value:
        return None
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _ts_time(timestamp: str) -> datetime:
    return datetime.strptime(timestamp[:14].ljust(14, "0"), "%Y%m%d%H%M%S").replace(tzinfo=UTC)


def _original_headers(headers: httpx.Headers, length: int) -> list[tuple[str, str]]:
    """The archived response's own headers, as the origin sent them.

    `X-Archive-Orig-*` with the prefix stripped; `Content-Type` and
    `Content-Encoding` as served, because Wayback passes those through rather
    than prefixing them. Hop-by-hop headers dropped and `Content-Length`
    recomputed: the original was often chunked and had none.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, value in headers.multi_items():
        lower = name.lower()
        if lower.startswith("x-archive-orig-"):
            original = name[len("x-archive-orig-") :]
            if original.lower() in HOP_BY_HOP or original.lower() in (
                "content-type",
                "content-encoding",
            ):
                continue
            out.append((original, value))
            seen.add(original.lower())
    for name in ("Content-Type", "Content-Encoding", "Location"):
        value = headers.get(name)
        if value and name.lower() not in seen:
            out.append((name, value))
    out.append(("Content-Length", str(length)))
    return out


def _warc_bytes(warc_dir: Path) -> int:
    total = 0
    if warc_dir.is_dir():
        for path in warc_dir.glob("*.warc.gz"):
            with contextlib.suppress(OSError):
                total += path.stat().st_size
    return total


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        sys.stderr.write("usage: python -m cairn.engines.wayback <job.json>\n")
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
    finally:
        runner.client.close()


if __name__ == "__main__":
    sys.exit(main())
