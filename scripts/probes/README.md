# Probes

Scripts that answered a design question by measuring, rather than by reading
documentation and hoping. They are kept because the answers have a shelf life:
each one is pinned to a version of something outside this repo, and the right
move when that version moves is to run the probe again rather than to assume.

Not part of the test suite. They need Docker and a few minutes, they talk to
real images, and a CI run is the wrong place for either.

## `resume_probe.py` — does an interrupted crawl leave resumable state?

Answers the question pause/resume rests on: browsertrix's docs say state is
written when a crawl is "interrupted" without naming a signal, and Cairn stops
an engine container with Docker's stop, which is **SIGTERM**. An implementation
that only handled SIGINT would have failed as an empty directory rather than an
error — silently, and only in production.

Three arms against a local fixture site, each interrupted six pages in.

**Measured on `webrecorder/browsertrix-crawler:1.14.1`, 2026-08-15:**

| Arm | Pages crawled first | State written |
|---|---|---|
| SIGTERM, default `--saveState` | 6 | yes — one file, 7,500 bytes |
| SIGINT, default | 6 | yes |
| SIGTERM, `--saveState always --saveStateInterval 5` | 6 | yes — 4 snapshots |

So `--saveState` defaulting to `partial` already writes the file on the signal
Cairn sends, with no flag change: it had always been written into the job's
temp directory and deleted moments later along with it. Arm 3 shows periodic
snapshots work, which is what a pause would need to survive a crash rather than
only a clean stop. An interrupted crawl exits **11**, not 0.

The state itself is a real queue, not a marker — `finished:` held the six
completed URLs and `queued:` the pending ones with their depth and seed id.

**The negative control is the point.** A run that reached zero pages produces an
empty `crawls/` directory for reasons that have nothing to do with signals, and
looks exactly like "SIGTERM does not work". Every arm asserts it crawled real
pages first and refuses to draw a conclusion otherwise — twice earlier in this
project a fixture the container could not reach nearly proved the opposite of
the truth.

## `resume_probe2.py` — and does that state actually resume?

A state file that replays the whole crawl would make "pause" a lie: the archive
doubles and nothing is saved. So the proof is negative — pages already finished
must not be fetched again.

Run `resume_probe.py` first; this reads its output directory.

**Measured on the same image and day:**

```
pages this run       : 6
  already-done again : 0
  new pages          : 6   ← resumed at p6, exactly where it stopped
warcs before / after : 1 / 2
exit code            : 0
```

It picked up the queue and wrote a **second WARC beside the first** rather than
rewriting it, which is what makes resuming into the same capture directory the
simple option: replay indexes across WARCs and never merges them
([D2](../../docs/00-decisions.md)), so the two halves of an interrupted crawl
need no reconciling.

Command-line options are not persisted in the state file and had to be
reapplied alongside `--config` — costless here, since `_argv()` rebuilds them
from the scope on every run anyway.

## Running them

Needs Docker and the pinned image:

```bash
python scripts/probes/resume_probe.py && python scripts/probes/resume_probe2.py
```
