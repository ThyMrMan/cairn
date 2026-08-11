"""Request and response models.

Response models are explicit rather than serialized ORM objects: a model
gains a `cookies_enc` column one day and an implicit serializer publishes it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Ok(BaseModel):
    ok: bool = True


# ── health ───────────────────────────────────────────────────────────────


class HealthResponse(BaseModel):
    """Unauthenticated. Must leak nothing beyond liveness (docs/09)."""

    status: str
    version: str
    db: bool
    setup_complete: bool
    disk_free_bytes: int | None = None


class VersionResponse(BaseModel):
    """Authenticated, because `build` names the exact commit.

    On an internet-exposed instance that is a direct index into which known
    bugs are present, which is not something an unauthenticated liveness
    probe should hand out. `/health` keeps the bare version it always had.
    """

    version: str
    build: str
    built_at: str | None = None
    label: str


# ── setup ────────────────────────────────────────────────────────────────


class SetupStatus(BaseModel):
    setup_complete: bool
    password_min_length: int


class SetupRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


# ── auth ─────────────────────────────────────────────────────────────────


class LoginRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)
    totp: str | None = Field(default=None, max_length=32)


class LoginResponse(BaseModel):
    username: str
    expires_at: datetime
    totp_enabled: bool


class MeResponse(BaseModel):
    username: str
    totp_enabled: bool
    created_at: datetime
    last_login_at: datetime | None


class PasswordChangeRequest(BaseModel):
    current: str = Field(min_length=1, max_length=1024)
    new: str = Field(min_length=1, max_length=1024)


class PasswordChangeResponse(BaseModel):
    revoked_sessions: int


class TotpSetupResponse(BaseModel):
    secret: str
    provisioning_uri: str


class TotpConfirmRequest(BaseModel):
    code: str = Field(min_length=6, max_length=10)


class TotpConfirmResponse(BaseModel):
    recovery_codes: list[str]


class TotpDisableRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)
    code: str = Field(min_length=6, max_length=32)


class SessionInfo(BaseModel):
    id: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    user_agent: str | None
    ip: str | None
    current: bool


# ── audit ────────────────────────────────────────────────────────────────


class AuditEntry(BaseModel):
    id: int
    ts: datetime
    actor: str | None
    action: str
    target: str | None
    ip: str | None


class Page[T](BaseModel):
    items: list[T]
    total: int
    page: int
    per_page: int


# ── scope ────────────────────────────────────────────────────────────────


class HostRuleModel(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    crawl_pages: bool = False
    fetch_assets: bool = True
    path_prefix: str | None = None
    allow_extensionless: bool = False


class ScopeModel(BaseModel):
    """The resolved scope (docs/04). `seeds` is derived from the site."""

    hosts: list[HostRuleModel] = Field(default_factory=list, max_length=500)
    exclude_hosts: list[str] = Field(default_factory=list, max_length=500)
    accept_patterns: list[str] = Field(default_factory=list, max_length=200)
    reject_patterns: list[str] = Field(default_factory=list, max_length=200)
    path_prefix: str | None = None
    max_depth: int | None = Field(default=None, ge=0, le=100)
    max_pages: int | None = Field(default=None, ge=1)
    max_bytes: int | None = Field(default=None, ge=0)
    obey_robots: bool = True
    politeness: dict[str, Any] = Field(default_factory=dict)


class ScopeResponse(ScopeModel):
    seeds: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    wget_preview: list[str] = Field(default_factory=list)


# ── folders & tags ───────────────────────────────────────────────────────


class FolderCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=128)
    parent_id: int | None = None


class FolderUpdate(BaseModel):
    """A rename and a reparent are both a path change, so they share a shape.

    `parent_id` needs three states — absent (leave it), a number (move there),
    and null (move to the top level) — which JSON gives us only if absence is
    distinguishable from null. Hence the sentinel rather than `int | None`.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=128)
    parent_id: int | None = None
    reparent: bool = False
    sort_order: int | None = Field(default=None, ge=0, le=100_000)


class FolderNodeModel(BaseModel):
    id: int
    parent_id: int | None
    name: str
    slug: str
    path: str
    sort_order: int
    site_count: int
    total_site_count: int
    size_bytes: int
    total_size_bytes: int
    children: list[FolderNodeModel] = Field(default_factory=list)


FolderNodeModel.model_rebuild()


class MoveOutcome(BaseModel):
    """What happened, or what was queued to happen.

    A move is one `rename(2)` and finishes inside the request — except when
    the two ends are on different filesystems, where it is a byte copy and
    becomes a job. The client cannot predict which, so it is told: `done`
    carries the new path, `queued` carries a job to watch.
    """

    status: str  # done | queued
    method: str  # rename | copy | noop
    path: str
    job_id: int | None = None


class TagCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=64)
    color: str | None = Field(default=None, max_length=16)
    description: str | None = Field(default=None, max_length=1000)


class TagUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=64)
    color: str | None = Field(default=None, max_length=16)
    description: str | None = Field(default=None, max_length=1000)


class TagSummary(BaseModel):
    id: int
    name: str
    slug: str
    color: str | None
    description: str | None
    site_count: int


# ── saved views ──────────────────────────────────────────────────────────


class SavedViewCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=128)
    query: dict[str, Any] = Field(default_factory=dict)
    pinned: bool = False


class SavedViewUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=128)
    query: dict[str, Any] | None = None
    pinned: bool | None = None


class SavedViewSummary(BaseModel):
    id: int
    name: str
    query: dict[str, Any]
    query_string: str
    pinned: bool


# ── trash ────────────────────────────────────────────────────────────────


class TrashEntry(BaseModel):
    id: int
    slug: str
    title: str
    seed_url: str
    folder_path: str
    deleted_at: datetime | None
    size_bytes: int
    on_disk: bool
    purge_after_days: int | None


# ── bulk ─────────────────────────────────────────────────────────────────


class BulkSiteRequest(BaseModel):
    """Several sites, one change. Any combination of the three is allowed."""

    site_ids: list[int] = Field(min_length=1, max_length=500)
    add_tags: list[str] = Field(default_factory=list, max_length=20)
    remove_tags: list[str] = Field(default_factory=list, max_length=20)
    folder_id: int | None = None


class BulkSiteResponse(BaseModel):
    tagged: int
    untagged: int
    moved: int
    queued_job_ids: list[int] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)


# ── sites ────────────────────────────────────────────────────────────────


class SiteMoveRequest(BaseModel):
    folder_id: int


class SiteCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    seed_url: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(default=None, max_length=255)
    folder_id: int | None = None
    notes: str | None = Field(default=None, max_length=10_000)
    engine_id: str = Field(default="wget-warc", max_length=64)
    profile_id: int | None = None
    keep_mirror: bool = False
    tags: list[str] = Field(default_factory=list, max_length=50)


class SiteUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str | None = Field(default=None, max_length=255)
    notes: str | None = Field(default=None, max_length=10_000)
    folder_id: int | None = None
    engine_id: str | None = Field(default=None, max_length=64)
    engine_config: dict[str, Any] | None = None
    profile_id: int | None = None
    keep_mirror: bool | None = None
    tags: list[str] | None = Field(default=None, max_length=50)


class SiteSummary(BaseModel):
    id: int
    slug: str
    title: str
    seed_url: str
    primary_host: str
    folder_id: int
    folder_path: str
    status: str
    engine_id: str
    profile_id: int | None
    keep_mirror: bool
    tags: list[str]
    size_bytes: int
    url_count: int
    archive_path: str
    last_capture_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SiteDetail(SiteSummary):
    notes: str | None
    engine_config: dict[str, Any]
    scope: ScopeResponse
    capture_count: int
    running_job_id: int | None


# ── captures ─────────────────────────────────────────────────────────────


class CaptureRequest(BaseModel):
    kind: str = Field(default="full", pattern="^(full|incremental)$")
    extra_seeds: list[str] = Field(default_factory=list, max_length=1000)


class JobAccepted(BaseModel):
    job_id: int


class CaptureSummary(BaseModel):
    id: int
    site_id: int
    job_id: int | None
    kind: str
    engine_id: str
    engine_version: str | None
    dir_name: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    url_count: int
    error_count: int
    bytes_written: int


class CaptureDetail(CaptureSummary):
    artifacts: list[dict[str, Any]]
    manifest: dict[str, Any] | None


class CaptureUrlEntry(BaseModel):
    id: int
    url: str
    host: str
    status_code: int | None
    mime: str | None
    size_bytes: int | None
    is_revisit: bool
    fetched_at: datetime | None
    error: str | None


# ── jobs ─────────────────────────────────────────────────────────────────


class JobSummary(BaseModel):
    id: int
    type: str
    site_id: int | None
    site_title: str | None
    status: str
    progress: dict[str, Any] | None
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    attempts: int


# ── profiles ─────────────────────────────────────────────────────────────


class ProfileCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=128)
    mode: str = Field(default="cookies", pattern="^(none|cookies|userscript|interactive)$")
    user_agent: str | None = Field(default=None, max_length=512)
    hosts: list[str] = Field(default_factory=list, max_length=100)
    verify_url: str | None = Field(default=None, max_length=2048)
    notes: str | None = Field(default=None, max_length=10_000)


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=128)
    user_agent: str | None = Field(default=None, max_length=512)
    hosts: list[str] | None = Field(default=None, max_length=100)
    verify_url: str | None = Field(default=None, max_length=2048)
    notes: str | None = Field(default=None, max_length=10_000)


class CookieUploadResponse(BaseModel):
    """The parse report. Never contains cookie values (docs/06)."""

    cookie_count: int
    hosts_covered: list[str]
    session_cookies: int
    expired_cookies: int
    earliest_expiry: str | None
    sensitive: list[str]
    warnings: list[str]
    errors: list[str]
    ok: bool


class CoverageResponse(BaseModel):
    covered: dict[str, bool]
    warnings: list[str]


class InteractiveStart(BaseModel):
    url: str | None = Field(default=None, max_length=2048)


class InteractiveSession(BaseModel):
    """Where to connect, and the size the frames will be.

    The viewport goes to the client because it has to map a click on its
    canvas back to a coordinate in the page, and guessing that from the first
    frame would be wrong until a frame arrived — which, on a settled page,
    could be never.
    """

    session_id: str
    profile_id: int
    url: str
    width: int
    height: int


# ── engines ──────────────────────────────────────────────────────────────


class EngineSummary(BaseModel):
    id: str
    name: str
    version: str
    source: str
    description: str
    capabilities: dict[str, Any]
    enabled: bool
    error: str | None = None
