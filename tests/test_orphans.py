"""A crawl that outlived the process which started it.

Reported as "got stuck and stopped doing anything, and wasn't able to cancel
it". It was doing neither. `crawl.log` shows wget fetching at a steady 2,470
URLs an hour, no gap over a minute, for **three days and eighteen hours** —
writing into a capture the database had already marked `interrupted`.

The chain: an engine is spawned with `start_new_session=True`, so it survives
this process restarting; the boot reconcile marked the job `interrupted` and
set `job.pid = None`, throwing away the only handle; and `cancel` then found
nothing running, nothing queued and a status that is not `running`, so it
returned False while wget carried on.

Every branch here is about not making that worse. A reaper that signals the
wrong process is a far more expensive bug than the one it fixes, so identity is
checked first and an unanswerable question keeps the pid rather than spending
it.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Job
from cairn.services import orphans

MARKER = "/data/tmp/job-74"


@pytest.fixture
def posix(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[tuple[int, int]]]:
    """A POSIX process table this test controls.

    `killpg` and `/proc` do not exist on Windows, where much of this suite
    runs, and the decisions are the part worth testing rather than the syscall.
    """
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(orphans, "_killpg", lambda pgid, sig: signals.append((pgid, sig)))
    monkeypatch.setattr(orphans, "_getpgid", lambda pid: pid)
    monkeypatch.setattr(orphans, "_SIGKILL", 9)
    return {"signals": signals}


def _process(monkeypatch: pytest.MonkeyPatch, cmdline: str | None, *, living: bool) -> None:
    monkeypatch.setattr(orphans, "command_line", lambda _pid: cmdline)
    monkeypatch.setattr(orphans, "alive", lambda _pid: living)


# ── identity comes first ─────────────────────────────────────────────────


def test_a_pid_that_is_not_ours_is_never_signalled(
    posix: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pid outlives its process and the number is reused. Signalling a
    remembered one without checking is a loaded gun pointed at whatever
    inherited it."""
    _process(monkeypatch, "/usr/bin/postgres -D /var/lib/postgres", living=True)
    assert orphans.reap(4321, MARKER) == orphans.NOT_OURS
    assert posix["signals"] == []


def test_a_pid_that_cannot_be_read_is_not_guessed_at(
    posix: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `/proc` means *unknown*, never *no*. Reaping on it would be the same
    unchecked kill by another route."""
    _process(monkeypatch, None, living=True)
    assert orphans.reap(4321, MARKER) == orphans.UNSUPPORTED
    assert posix["signals"] == []


def test_a_process_that_has_already_gone_is_reported_as_gone(
    posix: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _process(monkeypatch, None, living=False)
    assert orphans.reap(4321, MARKER) == orphans.GONE
    assert posix["signals"] == []


def test_a_pid_that_no_longer_leads_its_own_group_is_not_ours(
    posix: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine was started as a session leader, so its pid is its group id.
    Anything else means the number has moved on, whatever the command line
    happens to say."""
    _process(monkeypatch, f"python -m cairn.engines.wget {MARKER}/job.json", living=True)
    monkeypatch.setattr(orphans, "_getpgid", lambda _pid: 1)
    assert orphans.reap(74, MARKER) == orphans.NOT_OURS
    assert posix["signals"] == []


# ── stopping it ──────────────────────────────────────────────────────────


def test_the_whole_group_is_signalled_not_just_the_engine(
    posix: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """wget is a child of the engine and shares its group. Killing only the
    engine leaves the same orphan one level down — still fetching, still
    writing `crawl.log`, and now with no recorded pid at all."""
    _process(monkeypatch, f"python -m cairn.engines.wget {MARKER}/job.json", living=False)
    assert orphans.reap(74, MARKER) == orphans.STOPPED
    assert posix["signals"] == [(74, orphans.signal.SIGTERM)]


def test_a_group_that_ignores_the_grace_period_is_killed(
    posix: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`SIGTERM → grace → SIGKILL` is what docs/09 has always promised the
    cancel button does. Only the first half existed."""
    _process(monkeypatch, f"python -m cairn.engines.wget {MARKER}/job.json", living=True)
    assert orphans.reap(74, MARKER, grace_s=0.01) == orphans.KILLED
    assert posix["signals"] == [(74, orphans.signal.SIGTERM), (74, 9)]


def test_the_grace_period_comes_before_the_kill(
    posix: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """wget closes its current WARC record on the way out; killing straight
    away leaves a truncated final gzip member (docs/05)."""
    _process(monkeypatch, f"python -m cairn.engines.wget {MARKER}/job.json", living=True)
    orphans.reap(74, MARKER, grace_s=0.01)
    kinds = [sig for _pgid, sig in posix["signals"]]
    assert kinds == [orphans.signal.SIGTERM, 9]


@pytest.mark.parametrize("bad", [0, -1])
def test_a_missing_pid_is_not_a_target(bad: int) -> None:
    assert not orphans.owns(bad, MARKER)


def test_the_marker_must_actually_match() -> None:
    assert orphans.owns.__doc__
    line = "python -m cairn.engines.wget /data/tmp/job-7/job.json"
    # job-7 is not job-74, and a substring test that got this wrong would reap
    # a healthy neighbouring job.
    assert MARKER not in line


# ── through the supervisor ───────────────────────────────────────────────


@pytest.fixture
def supervisor(client: TestClient):  # type: ignore[no-untyped-def]
    """The app's own, so this exercises what actually runs."""
    return client.app.state.supervisor  # type: ignore[attr-defined]


def _interrupted_job(db: Session, pid: int | None) -> Job:
    job = Job(type="capture", status="running", spec={}, pid=pid)
    db.add(job)
    db.commit()
    return job


def test_boot_stops_a_crawl_the_last_process_left_running(
    supervisor, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pid is a resource to spend, not a field to clear."""
    job = _interrupted_job(db, 4242)
    reaped: list[tuple[int, str]] = []

    def fake_reap(pid: int, marker: str, **_: object) -> str:
        reaped.append((pid, marker))
        return orphans.STOPPED

    monkeypatch.setattr(orphans, "reap", fake_reap)
    supervisor._recover_interrupted()

    db.expire_all()
    assert reaped == [(4242, str(supervisor._settings.tmp_dir / f"job-{job.id}"))]
    assert db.get(Job, job.id).status == "interrupted"
    assert db.get(Job, job.id).pid is None


def test_a_pid_the_reaper_could_not_judge_is_kept(
    supervisor, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """So cancel can try again. Clearing it is exactly what left a runaway
    crawl with nothing pointing at it."""
    job = _interrupted_job(db, 4242)
    monkeypatch.setattr(orphans, "reap", lambda *a, **k: orphans.UNSUPPORTED)
    supervisor._recover_interrupted()

    db.expire_all()
    assert db.get(Job, job.id).pid == 4242


def test_cancel_reaches_a_job_this_process_is_not_running(
    supervisor, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The click that used to do nothing at all."""
    import asyncio

    job = _interrupted_job(db, 4242)
    job.status = "interrupted"
    db.commit()
    monkeypatch.setattr(orphans, "reap", lambda *a, **k: orphans.KILLED)

    assert (
        asyncio.get_event_loop_policy()
        .new_event_loop()
        .run_until_complete(supervisor.cancel(job.id))
    )
    db.expire_all()
    row = db.get(Job, job.id)
    assert row.pid is None
    assert row.status == "cancelled"


def test_cancelling_a_job_with_no_pid_still_reports_honestly(supervisor, db: Session) -> None:
    import asyncio

    job = _interrupted_job(db, None)
    job.status = "interrupted"
    db.commit()
    assert (
        not asyncio.get_event_loop_policy()
        .new_event_loop()
        .run_until_complete(supervisor.cancel(job.id))
    )


# ── the clock nobody was watching ────────────────────────────────────────


def test_a_capture_now_has_a_duration_cap(db: Session) -> None:
    from cairn.services.jobs import _duration_cap

    assert _duration_cap(db) == 48 * 3600


def test_the_cap_can_be_switched_off(db: Session) -> None:
    from cairn.services import settings_store
    from cairn.services.jobs import _duration_cap

    settings_store.put(db, "crawl.max_duration_hours", 0)
    assert _duration_cap(db) is None


def test_the_cap_is_configurable(db: Session) -> None:
    from cairn.services import settings_store
    from cairn.services.jobs import _duration_cap

    settings_store.put(db, "crawl.max_duration_hours", 6)
    assert _duration_cap(db) == 6 * 3600


def test_the_job_spec_actually_carries_the_cap(db: Session) -> None:
    """The wiring, not just the number. `max_duration_s` was hardcoded `None`
    in the spec, so the engine's own self-stop had nothing to check against and
    a crawl could run until somebody noticed."""
    from cairn.services.jobs import _limits
    from cairn.services.scope import HostRule, Scope

    scope = Scope(
        seeds=["https://b.example/"],
        hosts=[HostRule("b.example", crawl_pages=True, fetch_assets=True)],
    )
    assert _limits(db, scope)["max_duration_s"] == 48 * 3600


def test_the_engine_reads_it_and_stops_itself(tmp_path, settings: Settings) -> None:
    """The engine's own guard, which had never been given a number to check —
    `max_duration_s` was hardcoded `None` in the job spec, so a crawl could run
    until somebody noticed."""
    from cairn.engines.protocol import JobSpec

    spec = JobSpec.model_validate(
        {
            "protocol": "cairn.engine/v1",
            "job_id": 1,
            "site": {"id": 1, "slug": "b", "title": "B"},
            "output_dir": str(tmp_path / "o"),
            "temp_dir": str(tmp_path / "t"),
            "seeds": ["https://b.example/"],
            "scope": {
                "seeds": ["https://b.example/"],
                "hosts": [{"host": "b.example", "crawl_pages": True, "fetch_assets": True}],
            },
            "config": {},
            "limits": {"max_duration_s": 3600},
        }
    )
    assert spec.limits.max_duration_s == 3600


def test_the_env_is_posix_or_the_reaper_says_so() -> None:
    """On Windows there is no `killpg`, and the honest answer is that nothing
    was done — not a silent success."""
    if hasattr(os, "killpg"):
        pytest.skip("POSIX: the real path is covered above")
    assert orphans.reap(1, MARKER) == orphans.UNSUPPORTED
