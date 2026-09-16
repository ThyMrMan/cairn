"""Short write transactions that wait out a busy database.

SQLite has one writer at a time. `busy_timeout` makes a second writer wait,
but only for five seconds, and then it raises `database is locked` — which in
the job supervisor used to unwind straight through the task that was reading a
crawl's output. Measured on a live instance: a post-processor held the write
lock for about four minutes, the capture running beside it hit the timeout on
its next batch of URL rows, and that capture's job was never heard from again.
wget carried on for eight more days, unwatched.

Two rules follow, and this module is the second.

**Keep write transactions short.** Nothing slow — hashing a WARC, walking a
directory, starting a browser — may run while a transaction holds the lock.

**A write that must land waits for as long as it takes, and does not lose its
place.** Retrying a failed `commit` is not possible: SQLAlchemy rolls the
session back and the pending changes go with it. So the unit that is retried is
a function that *builds* the transaction from values it was given, run against
a fresh session each time. Anything passed to `write` must therefore be
idempotent, and must load what it changes rather than holding on to objects
from an earlier attempt.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from typing import Protocol

from sqlalchemy import exc as sa_exc
from sqlalchemy.orm import Session, sessionmaker

from cairn.logging import get_logger

log = get_logger(__name__)


# How long a write that has to land may keep trying. Generous on purpose: the
# callers are the ones recording how a job ended, and giving up leaves a row
# that says `running` about something that is not. The longest lock measured on
# a real instance was about four minutes.
WRITE_BUDGET_S = 600.0
FIRST_DELAY_S = 0.5
MAX_DELAY_S = 15.0

# SQLite's primary result codes. Extended codes carry these in their low byte —
# SQLITE_BUSY_SNAPSHOT is 517, SQLITE_BUSY_TIMEOUT 773.
_SQLITE_BUSY = 5
_SQLITE_LOCKED = 6
_BUSY_WORDS = ("database is locked", "database table is locked", "database is busy")


def is_busy(exc: BaseException) -> bool:
    """Whether `exc` means "another connection has the lock; try again".

    Accepts the driver's exception or SQLAlchemy's wrapper around it. Only
    contention counts — a full disk or a missing table is not going to get
    better by waiting, and retrying it would turn an error into a hang.
    """
    raw = getattr(exc, "orig", None) or exc
    code = getattr(raw, "sqlite_errorcode", None)
    if isinstance(code, int):
        return (code & 0xFF) in (_SQLITE_BUSY, _SQLITE_LOCKED)
    message = str(raw).lower()
    return any(word in message for word in _BUSY_WORDS)


def is_transient(exc: BaseException) -> bool:
    """Whether `exc` is the database refusing, rather than objecting to the data.

    Wider than `is_busy`: a full disk or an unreachable file may clear, and a
    caller that can hold its rows in memory is right to try again later. A
    constraint violation will not clear, however long anybody waits.
    """
    raw = getattr(exc, "orig", None) or exc
    return isinstance(exc, (sa_exc.OperationalError, sa_exc.TimeoutError)) or isinstance(
        raw, sqlite3.OperationalError
    )


class Writer(Protocol):
    """`write`, with the session factory already bound."""

    def __call__[T](self, apply: Callable[[Session], T], *, what: str) -> T: ...


def write[T](
    sessions: sessionmaker[Session],
    apply: Callable[[Session], T],
    *,
    what: str,
    budget_s: float = WRITE_BUDGET_S,
) -> T:
    """Run `apply` in its own transaction, retrying while the database is busy.

    Blocking — call it from a worker thread, never from the event loop. Any
    error other than contention is raised on the first attempt.
    """
    deadline = time.monotonic() + budget_s
    delay = FIRST_DELAY_S
    attempt = 0
    while True:
        attempt += 1
        try:
            with sessions() as session:
                result = apply(session)
                session.commit()
                return result
        except Exception as exc:
            remaining = deadline - time.monotonic()
            if not is_busy(exc) or remaining <= 0:
                raise
            wait = min(delay, remaining)
            log.warning(
                "database busy; retrying",
                extra={"what": what, "attempt": attempt, "retry_in_s": round(wait, 2)},
            )
            time.sleep(wait)
            delay = min(delay * 2, MAX_DELAY_S)
