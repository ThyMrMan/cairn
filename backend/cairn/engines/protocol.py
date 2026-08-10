"""The engine contract: job spec in, NDJSON events out (docs/05).

Both sides of the boundary live here so they cannot drift. Core imports this
to write `job.json` and parse stdout; the built-in engine imports it to read
the spec and emit events. A third-party engine in another language reimplements
it from the docs — which is why every field is plain JSON and nothing depends
on Python types crossing the boundary.

Contract rules core enforces:

  - Exit 0 *and* a `result` line means success. Non-zero, or exiting without
    a `result`, is a failure regardless of what came before.
  - Malformed stdout lines are counted and skipped, never fatal. An engine
    that prints a stray line must not kill a six-hour crawl.
  - Artifact paths are relative to `output_dir` and may not escape it.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Annotated, Any, Literal, TextIO

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from cairn.db.types import to_iso, utcnow

PROTOCOL_VERSION = "cairn.engine/v1"
POSTPROCESSOR_VERSION = "cairn.postprocessor/v1"

JOB_SPEC_FILE = "job.json"
SEED_FILE = "seeds.txt"

# Terminal statuses an engine may report. `partial` is a first-class success:
# a crawl that got 1,835 of 1,847 pages is an archive with 12 known gaps, not
# a failure (docs/05).
ResultStatus = Literal["ok", "partial", "failed"]

# Warning codes core reacts to rather than merely displaying.
WARN_INTERSTITIAL = "interstitial_detected"
WARN_LAZY_IMAGES = "lazy_images_detected"
WARN_MISSING_ASSETS = "missing_assets"
WARN_SCOPE_REJECTED = "scope_rejected"


# ── job spec (core → engine) ─────────────────────────────────────────────


class JobSite(BaseModel):
    id: int
    slug: str
    title: str


class JobAuth(BaseModel):
    """Auth material for this run.

    `cookies_file` points into the job's temp directory and is deleted when
    the job ends, including after a crash via the boot sweep (docs/06). The
    file itself is never in the archive tree.
    """

    cookies_file: str | None = None
    user_agent: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)


class JobIncremental(BaseModel):
    dedup_cdx: str | None = None


class JobLimits(BaseModel):
    max_bytes: int | None = None
    max_duration_s: int | None = None
    free_space_floor_bytes: int | None = None


class JobSpec(BaseModel):
    """What core writes to `job.json` and passes as argv[1]."""

    model_config = ConfigDict(extra="allow")

    protocol: str = PROTOCOL_VERSION
    job_id: int
    job_type: str = "capture"
    site: JobSite
    output_dir: str
    temp_dir: str
    seeds: list[str] = Field(default_factory=list)
    seed_file: str | None = SEED_FILE
    scope: dict[str, Any] = Field(default_factory=dict)
    auth: JobAuth = Field(default_factory=JobAuth)
    incremental: JobIncremental = Field(default_factory=JobIncremental)
    config: dict[str, Any] = Field(default_factory=dict)
    limits: JobLimits = Field(default_factory=JobLimits)

    @classmethod
    def load(cls, path: str) -> JobSpec:
        with open(path, encoding="utf-8") as fh:
            return cls.model_validate(json.load(fh))


# ── events (engine → core) ───────────────────────────────────────────────


class _Event(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ts: datetime | None = None


class StartedEvent(_Event):
    type: Literal["started"]
    tool_version: str | None = None


class LogEvent(_Event):
    type: Literal["log"]
    level: str = "info"
    msg: str = ""


class UrlEvent(_Event):
    type: Literal["url"]
    url: str
    status: int | None = None
    mime: str | None = None
    size: int | None = None
    digest: str | None = None
    revisit: bool = False
    error: str | None = None


class ProgressEvent(_Event):
    type: Literal["progress"]
    done: int = 0
    total: int | None = None
    bytes: int = 0
    rate_bps: float | None = None
    eta_s: float | None = None


class ArtifactEvent(_Event):
    type: Literal["artifact"]
    kind: str
    path: str
    size: int | None = None
    sha256: str | None = None


class WarningEvent(_Event):
    type: Literal["warning"]
    code: str
    msg: str = ""
    url: str | None = None
    detail: dict[str, Any] | None = None


class ResultEvent(_Event):
    type: Literal["result"]
    status: ResultStatus
    stats: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


Event = Annotated[
    StartedEvent | LogEvent | UrlEvent | ProgressEvent | ArtifactEvent | WarningEvent | ResultEvent,
    Field(discriminator="type"),
]

_EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


def parse_event(line: str) -> Event | None:
    """Parse one NDJSON line, returning None for anything unusable.

    Never raises. A stray `print()` in an engine, a partially flushed line, or
    an unknown event type from a newer engine all have to be survivable.
    """
    line = line.strip()
    if not line or line[0] != "{":
        return None
    try:
        return _EVENT_ADAPTER.validate_json(line)
    except (ValidationError, ValueError):
        return None


# ── emission (engine side) ───────────────────────────────────────────────


class EventWriter:
    """Emits NDJSON on stdout, flushing every line.

    Without the flush, progress and log events sit in a pipe buffer and the
    live log in the UI stays empty until the crawl ends — which is exactly
    when nobody needs it any more.
    """

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def emit(self, **fields: Any) -> None:
        fields.setdefault("ts", to_iso(utcnow()))
        self._stream.write(json.dumps(fields, separators=(",", ":"), default=str) + "\n")
        self._stream.flush()

    # Convenience wrappers — keep event names in one place.

    def started(self, tool_version: str | None = None) -> None:
        self.emit(type="started", tool_version=tool_version)

    def log(self, msg: str, level: str = "info") -> None:
        self.emit(type="log", level=level, msg=msg)

    def url(self, url: str, **fields: Any) -> None:
        self.emit(type="url", url=url, **fields)

    def progress(self, done: int, **fields: Any) -> None:
        self.emit(type="progress", done=done, **fields)

    def artifact(self, kind: str, path: str, **fields: Any) -> None:
        self.emit(type="artifact", kind=kind, path=path, **fields)

    def warning(self, code: str, msg: str, **fields: Any) -> None:
        self.emit(type="warning", code=code, msg=msg, **fields)

    def result(self, status: ResultStatus, stats: dict[str, Any] | None = None, **f: Any) -> None:
        self.emit(type="result", status=status, stats=stats or {}, **f)


__all__ = [
    "JOB_SPEC_FILE",
    "PROTOCOL_VERSION",
    "SEED_FILE",
    "WARN_INTERSTITIAL",
    "WARN_LAZY_IMAGES",
    "WARN_MISSING_ASSETS",
    "WARN_SCOPE_REJECTED",
    "ArtifactEvent",
    "Event",
    "EventWriter",
    "JobAuth",
    "JobIncremental",
    "JobLimits",
    "JobSite",
    "JobSpec",
    "LogEvent",
    "ProgressEvent",
    "ResultEvent",
    "ResultStatus",
    "StartedEvent",
    "UrlEvent",
    "WarningEvent",
    "parse_event",
]
