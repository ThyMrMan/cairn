"""A slow, careful HTTP client for servers that block impatient clients.

Built for archive.org first (docs/17) and meant to serve discovery and feeds
later (audit #5, docs/18). The rules, all of which come from something that
went wrong:

- **One request at a time per host, with a gap.** The gap is shared by every
  client in this process, so two jobs talking to the same host queue behind
  each other rather than doubling the rate. Across processes, the job queue's
  per-host rule does the same job (`jobs._host_for_job`).
- **429 and 503 are waits, not failures.** `Retry-After` is honoured when sent;
  otherwise the wait doubles from `backoff_start_s` up to `backoff_max_s`.
- **A run of 429s stops the client** (`RateLimitedError`). Hammering an IP-level
  block is how a temporary block becomes a long one.
- **A disguised 429 counts as one.** Some sites redirect to an error page and
  serve it with 200 (fictionmania's `/err429.html`). `soft_429` is a pattern
  for that page's URL; a response that lands on it is treated as a 429.
- **An HTML body where data was expected is an outage page**, not data
  (archive.org's "Temporarily Offline" comes back as HTML, sometimes with 200).
- **Raw mode keeps the bytes as sent.** `id_` snapshots arrive still gzipped,
  and their digest is computed over those bytes; letting the client decode
  them breaks the digest and makes the stored headers lie.

Everything that waits does so in short slices and checks `should_stop`, so a
cancel is noticed within a second even in the middle of an hour's backoff.
"""

from __future__ import annotations

import email.utils
import random
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from cairn import __version__
from cairn.logging import get_logger

log = get_logger(__name__)

USER_AGENT = f"Cairn/{__version__} (self-hosted personal archiver; one request at a time)"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
DEFAULT_MAX_BODY_BYTES = 256 * 1024 * 1024


class PoliteError(Exception):
    """A request that could not be completed within its attempts."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RateLimitedError(PoliteError):
    """The server kept saying "slow down"; the client stopped asking."""


class StopRequestedError(Exception):
    """`should_stop` said so. Not an error: the caller asked."""


@dataclass(slots=True)
class Pacing:
    min_gap_s: float = 6.0
    jitter_s: float = 3.0
    backoff_start_s: float = 60.0
    backoff_max_s: float = 3600.0
    # Consecutive 429s (real or disguised) before giving up. Not reset by a
    # 5xx, which says nothing about whether the block has lifted.
    max_consecutive_429: int = 5
    # Attempts per request across every retryable outcome.
    max_attempts: int = 8
    timeout_s: float = 120.0

    @classmethod
    def per_minute(cls, rate: float, **overrides: float | int) -> Pacing:
        """A pacing whose average rate is `rate` requests per minute."""
        rate = max(float(rate), 0.1)
        jitter = float(overrides.pop("jitter_s", 3.0))
        gap = max(60.0 / rate - jitter / 2, 0.0)
        return cls(min_gap_s=gap, jitter_s=jitter, **overrides)  # type: ignore[arg-type]


@dataclass(slots=True)
class Response:
    url: str
    status: int
    headers: httpx.Headers
    body: bytes
    history: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Slot:
    """One host's turn-taking: a lock, and when the next request may go."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    ready_at: float = float("-inf")


# Shared across clients, keyed by (host, clock) so a test's fake clock never
# inherits a real deadline.
_HOSTS: dict[tuple[str, object], _Slot] = {}
_HOSTS_LOCK = threading.Lock()


def _host_slot(host: str, clock: object) -> _Slot:
    with _HOSTS_LOCK:
        slot = _HOSTS.get((host, clock))
        if slot is None:
            slot = _HOSTS[(host, clock)] = _Slot()
        return slot


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> float | None:
    """`Retry-After` as seconds: either a number or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - (now or datetime.now(UTC))).total_seconds(), 0.0)


def looks_like_html(body: bytes) -> bool:
    head = body.lstrip()[:64].lower()
    return head.startswith((b"<!doctype html", b"<html", b"<head"))


class PoliteClient:
    def __init__(
        self,
        pacing: Pacing | None = None,
        *,
        user_agent: str = USER_AGENT,
        soft_429: str | None = None,
        should_stop: Callable[[], bool] | None = None,
        on_wait: Callable[[str, float], None] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ) -> None:
        self.pacing = pacing or Pacing()
        self._soft_429 = re.compile(soft_429, re.I) if soft_429 else None
        self._should_stop = should_stop or (lambda: False)
        self._on_wait = on_wait or (lambda _why, _s: None)
        self._sleep = sleep
        self._clock = clock
        self._max_body = max_body_bytes
        self._consecutive_429 = 0
        self.requests = 0
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=self.pacing.timeout_s,
            follow_redirects=True,
            max_redirects=5,
            transport=transport,
        )

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ── waiting ──────────────────────────────────────────────────────────

    def _check_stop(self) -> None:
        if self._should_stop():
            raise StopRequestedError

    def wait(self, seconds: float, why: str = "") -> None:
        """Sleep, in slices, noticing a stop request within a second."""
        if seconds <= 0:
            self._check_stop()
            return
        if why:
            self._on_wait(why, seconds)
        end = self._clock() + seconds
        while True:
            self._check_stop()
            left = end - self._clock()
            if left <= 0:
                return
            self._sleep(min(left, 1.0))

    def _take_turn(self, host: str) -> None:
        """Wait for this host's gap, then claim the next slot."""
        slot = _host_slot(host, self._clock)
        while True:
            with slot.lock:
                ready_at = slot.ready_at
                now = self._clock()
                if now >= ready_at:
                    gap = self.pacing.min_gap_s + random.uniform(0, self.pacing.jitter_s)  # noqa: S311 — jitter, not security
                    slot.ready_at = now + gap
                    return
            self.wait(ready_at - now)

    def _push_back(self, host: str, seconds: float) -> None:
        """Hold every client of this host back, not only this one."""
        slot = _host_slot(host, self._clock)
        with slot.lock:
            slot.ready_at = max(slot.ready_at, self._clock() + seconds)

    # ── requests ─────────────────────────────────────────────────────────

    def get(
        self,
        url: str,
        *,
        params: dict[str, str | int] | None = None,
        raw: bool = False,
        reject_html: bool = False,
        accept_status: Callable[[int], bool] | None = None,
        follow_redirects: bool = True,
    ) -> Response:
        """GET with pacing and retries.

        `accept_status` decides which final statuses are answers rather than
        retries; by default anything not in `RETRY_STATUSES`. A 404 is an
        answer — the caller decides what it means.
        """
        host = (urlsplit(url).hostname or "").lower()
        accept = accept_status or (lambda s: s not in RETRY_STATUSES)
        backoff = self.pacing.backoff_start_s
        last_problem = ""
        last_status: int | None = None
        for attempt in range(1, self.pacing.max_attempts + 1):
            self._take_turn(host)
            self._check_stop()
            wait_for: float | None = None
            try:
                response = self._fetch(url, params=params, raw=raw, follow=follow_redirects)
            except httpx.HTTPError as exc:
                last_problem, last_status = f"{type(exc).__name__}: {exc}", None
            else:
                self.requests += 1
                last_status = response.status
                soft = self._is_soft_429(response)
                if response.status == 429 or soft:
                    self._consecutive_429 += 1
                    last_problem = "429 (disguised as an error page)" if soft else "429"
                    if self._consecutive_429 >= self.pacing.max_consecutive_429:
                        raise RateLimitedError(
                            f"{host} answered {self._consecutive_429} rate-limit responses in a "
                            "row; stopped so the block can lift",
                            status=429,
                        )
                    wait_for = retry_after_seconds(response.headers.get("retry-after"))
                elif not accept(response.status):
                    last_problem = f"HTTP {response.status}"
                    wait_for = retry_after_seconds(response.headers.get("retry-after"))
                elif reject_html and looks_like_html(response.body):
                    # archive.org's outage page, served where data belongs.
                    last_problem = f"HTTP {response.status} with an HTML page instead of data"
                else:
                    self._consecutive_429 = 0
                    return response
            if attempt == self.pacing.max_attempts:
                break
            delay = wait_for if wait_for is not None else backoff
            delay = min(delay, self.pacing.backoff_max_s)
            log.info(
                "polite fetch retrying",
                extra={"url": url, "attempt": attempt, "problem": last_problem, "wait_s": delay},
            )
            self._push_back(host, delay)
            self.wait(delay, f"{last_problem}; retrying in {int(delay)} s")
            backoff = min(backoff * 2, self.pacing.backoff_max_s)
        raise PoliteError(
            f"gave up on {url} after {self.pacing.max_attempts} attempts ({last_problem})",
            status=last_status,
        )

    def _is_soft_429(self, response: Response) -> bool:
        if self._soft_429 is None:
            return False
        return any(self._soft_429.search(u) for u in (*response.history, response.url))

    def _fetch(
        self, url: str, *, params: dict[str, str | int] | None, raw: bool, follow: bool = True
    ) -> Response:
        with self._client.stream("GET", url, params=params, follow_redirects=follow) as r:
            chunks: list[bytes] = []
            size = 0
            stream = r.iter_raw() if raw else r.iter_bytes()
            for chunk in stream:
                size += len(chunk)
                if size > self._max_body:
                    raise PoliteError(f"{url}: body larger than {self._max_body} bytes")
                chunks.append(chunk)
            return Response(
                url=str(r.url),
                status=r.status_code,
                headers=r.headers,
                body=b"".join(chunks),
                history=[str(h.url) for h in r.history],
            )
