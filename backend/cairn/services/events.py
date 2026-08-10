"""In-process event bus behind the SSE endpoints (docs/09).

One publisher (the job supervisor), many short-lived subscribers (browser tabs
watching a live log). Two properties matter more than throughput:

**A reconnecting client must not silently lose events.** Every event gets a
monotonic id and lands in a bounded per-job ring buffer. A client reconnects
with `Last-Event-ID` and gets the gap replayed. A log viewer that quietly
drops the lines it missed is worse than one that admits it did — so when the
gap is larger than the buffer, the client is told.

**A stalled subscriber must not stall the crawl.** A browser tab that stops
reading cannot be allowed to apply backpressure to a six-hour capture, so each
subscriber has a bounded queue and is marked lagged rather than waited on.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from cairn.db.types import to_iso, utcnow
from cairn.logging import get_logger

log = get_logger(__name__)

BUFFER_PER_JOB = 500
SUBSCRIBER_QUEUE_SIZE = 1000
HEARTBEAT_SECONDS = 15.0

# SSE event names the frontend switches on.
EV_LOG = "log"
EV_PROGRESS = "progress"
EV_STATUS = "status"
EV_URL = "url"
EV_WARNING = "warning"
EV_ARTIFACT = "artifact"
EV_LAGGED = "lagged"


@dataclass(slots=True)
class BusEvent:
    id: int
    job_id: int | None
    event: str
    data: dict[str, Any]
    ts: str = field(default_factory=lambda: to_iso(utcnow()))

    def payload(self) -> dict[str, Any]:
        body = dict(self.data)
        body.setdefault("ts", self.ts)
        if self.job_id is not None:
            body.setdefault("job_id", self.job_id)
        return body


@dataclass(slots=True, eq=False)
class _Subscriber:
    """One connected SSE client.

    `eq=False` keeps identity hashing: a dataclass that generates __eq__ has
    __hash__ set to None and cannot go in a set, and two clients watching the
    same job are distinct subscribers regardless of matching fields.
    """

    queue: asyncio.Queue[BusEvent]
    job_id: int | None
    lagged: bool = False


class EventBus:
    def __init__(self, buffer_per_job: int = BUFFER_PER_JOB) -> None:
        self._seq = 0
        self._buffer_size = buffer_per_job
        self._buffers: dict[int, deque[BusEvent]] = {}
        self._subscribers: set[_Subscriber] = set()

    # ── publishing ───────────────────────────────────────────────────────

    def publish(self, job_id: int | None, event: str, data: dict[str, Any]) -> BusEvent:
        self._seq += 1
        item = BusEvent(id=self._seq, job_id=job_id, event=event, data=data)

        if job_id is not None:
            buffer = self._buffers.get(job_id)
            if buffer is None:
                buffer = self._buffers[job_id] = deque(maxlen=self._buffer_size)
            buffer.append(item)

        for sub in self._subscribers:
            if sub.job_id is not None and sub.job_id != job_id:
                continue
            try:
                sub.queue.put_nowait(item)
            except asyncio.QueueFull:
                # Drop, and remember to tell this subscriber it has a hole.
                sub.lagged = True
        return item

    def forget(self, job_id: int) -> None:
        """Release a finished job's buffer.

        Called once the job is terminal *and* its subscribers have drained, so
        a long-lived instance does not accumulate one ring buffer per capture
        it has ever run.
        """
        self._buffers.pop(job_id, None)

    # ── history ──────────────────────────────────────────────────────────

    def history(self, job_id: int, after_id: int = 0) -> list[BusEvent]:
        buffer = self._buffers.get(job_id)
        if not buffer:
            return []
        return [e for e in buffer if e.id > after_id]

    def has_gap(self, job_id: int, after_id: int) -> bool:
        """True when events between `after_id` and the buffer have been lost."""
        buffer = self._buffers.get(job_id)
        if not buffer:
            return False
        oldest = buffer[0].id
        return after_id > 0 and oldest > after_id + 1

    # ── subscription ─────────────────────────────────────────────────────

    @asynccontextmanager
    async def subscribe(self, job_id: int | None = None) -> AsyncIterator[_Subscriber]:
        sub = _Subscriber(queue=asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE), job_id=job_id)
        self._subscribers.add(sub)
        try:
            yield sub
        finally:
            self._subscribers.discard(sub)

    async def stream(
        self, sub: _Subscriber, *, heartbeat: float = HEARTBEAT_SECONDS
    ) -> AsyncIterator[BusEvent | None]:
        """Yield events, or None as a heartbeat when idle.

        The heartbeat is not decoration: an idle SSE connection through nginx
        or Cloudflare is closed after ~60 s of silence, and a capture can
        legitimately produce nothing for minutes while wget waits out a slow
        host.
        """
        while True:
            try:
                item = await asyncio.wait_for(sub.queue.get(), timeout=heartbeat)
            except TimeoutError:
                yield None
                continue
            if sub.lagged:
                sub.lagged = False
                yield BusEvent(
                    id=item.id,
                    job_id=sub.job_id,
                    event=EV_LAGGED,
                    data={"message": "Some events were dropped; reload for the full log."},
                )
            yield item


def format_sse(event: BusEvent | None) -> str:
    """Serialize for the wire. `None` becomes a comment-only heartbeat."""
    import json

    if event is None:
        return ": heartbeat\n\n"
    body = json.dumps(event.payload(), separators=(",", ":"), default=str)
    return f"id: {event.id}\nevent: {event.event}\ndata: {body}\n\n"
