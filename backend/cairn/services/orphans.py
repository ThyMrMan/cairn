"""Crawls that outlived the process which started them.

An engine is spawned with `start_new_session=True`, which is right: it gives
one handle — the process group — for the engine *and* the wget it runs, so
cancelling a job cannot signal the whole container. What it also means, and
what `_sweep_containers` used to claim was impossible, is that **a subprocess
does not die with its parent**. A new session is not in the parent's process
group, so nothing signals it; it is reparented and carries on.

Reported as a crawl that "got stuck and could not be cancelled". It was doing
neither. wget was fetching at a steady 2,470 URLs an hour, with no gap over a
minute, for **three days and eighteen hours** — writing `crawl.log` itself,
into a capture the database had already marked `interrupted`, with no way to
stop it from the UI. The boot reconcile had marked the job interrupted and set
`job.pid = None`, discarding the only handle anybody had; `cancel` then found
nothing in `_running`, nothing `queued`, and a status that is not `running`,
and returned False.

So the pid is a resource to spend rather than a field to clear.

**Identity is checked before anything is signalled.** A pid outlives its
process and the number is reused, so `os.killpg` on a remembered pid is a
loaded gun pointed at whatever inherited it. The job's own temp directory
appears in the engine's command line (`…/job-74/job.json`) and is unique to
that job, which makes the check exact rather than probable.

Where the check cannot be made — no `/proc`, which in practice means running
the tests on Windows — nothing is reaped and nothing is claimed. A reaper that
guesses is worse than one that says it could not tell.
"""

from __future__ import annotations

import contextlib
import os
import signal
import time
from pathlib import Path

from cairn.logging import get_logger

log = get_logger(__name__)

# How long the group gets to close its WARC before it is killed outright.
# wget's own termination path closes the current record first, which is what
# keeps the archive readable (docs/05); killing straight away leaves a
# truncated final gzip member.
GRACE_S = 20.0
POLL_S = 0.5

# POSIX-only, looked up rather than imported so that the guard and the use are
# the same fact. `killpg` is the whole mechanism — the engine leads its own
# session, so its group is the engine plus the wget under it — and a platform
# without it cannot do this at all rather than doing half of it.
_killpg = getattr(os, "killpg", None)
_getpgid = getattr(os, "getpgid", None)
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)

# Outcomes, so a caller can log the difference between "stopped it" and
# "could not tell whether it was ours".
STOPPED = "stopped"
KILLED = "killed"
GONE = "gone"
NOT_OURS = "not-ours"
UNSUPPORTED = "unsupported"


def command_line(pid: int) -> str | None:
    """A process's argv, or None where that cannot be read.

    None means *unknown*, never *no*. The caller must not reap on it.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", "replace")


def owns(pid: int, marker: str) -> bool:
    """Whether `pid` is still the process this job started."""
    if pid <= 0 or not marker:
        return False
    line = command_line(pid)
    return line is not None and marker in line


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover — someone else's, so not ours
        return True
    except OSError:  # pragma: no cover — Windows, where signal 0 is unsupported
        return False
    return True


def reap(pid: int, marker: str, *, grace_s: float = GRACE_S) -> str:
    """Stop an orphaned engine and the crawler under it.

    The **group**, not the process: the engine leads its own session, so the
    wget it spawned shares its group id and one `killpg` reaches both. Killing
    only the engine would leave wget running and still writing into the
    capture — the same orphan, one level down and with no recorded pid at all.
    """
    if _killpg is None or _getpgid is None:  # pragma: no cover — Windows
        return UNSUPPORTED
    if command_line(pid) is None:
        return GONE if not alive(pid) else UNSUPPORTED
    if not owns(pid, marker):
        return NOT_OURS

    try:
        pgid = _getpgid(pid)
    except (ProcessLookupError, OSError):
        return GONE
    # The engine was started as a session leader, so its pid is its group id.
    # Anything else means this pid is no longer that process, whatever its
    # command line says.
    if pgid != pid:
        return NOT_OURS

    with contextlib.suppress(ProcessLookupError, OSError):
        _killpg(pgid, signal.SIGTERM)

    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not alive(pid):
            return STOPPED
        time.sleep(POLL_S)

    with contextlib.suppress(ProcessLookupError, OSError):
        _killpg(pgid, _SIGKILL)
    return KILLED
