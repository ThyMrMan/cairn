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
    # A stat, not a column: the image is derived data on disk, and a boolean
    # the database owned could outlive the file it describes.
    has_thumbnail: bool = False


class SiteDetail(SiteSummary):
    notes: str | None
    engine_config: dict[str, Any]
    scope: ScopeResponse
    capture_count: int
    running_job_id: int | None
    # Whether this site's access profile carries a browsertrix browser
    # profile. The engine picker warns about a gate the chosen engine cannot
    # pass, and a tarball is the one thing that gets browsertrix through one.
    profile_has_browser_profile: bool = False
    # And whether it holds a cookie jar. A profile with a browser profile
    # and no jar is useless to wget, which is the mirror of the case above
    # and just as silent.
    profile_has_cookies: bool = False
    # The second pass this site's preset offers, or None. Present so the UI can
    # offer it without knowing which presets have one — a site whose scope was
    # built by hand simply has no pass and no button.
    companion_pass: dict[str, Any] | None = None
    # Which preset this site's scope was built from, `{id, name}`, or None for
    # a scope somebody assembled by hand. Shown on the pre-capture summary so
    # the two settings that decide what a multi-hour crawl costs — the engine
    # and the rules — are visible at the moment of starting it rather than
    # two tabs away.
    preset: dict[str, str] | None = None


# ── captures ─────────────────────────────────────────────────────────────


class CaptureRequest(BaseModel):
    kind: str = Field(default="full", pattern="^(full|incremental)$")
    extra_seeds: list[str] = Field(default_factory=list, max_length=1000)


class CompanionPassRequest(BaseModel):
    """Which second pass to run. Empty means "the one this site offers"."""

    pass_id: str = Field(default="", max_length=32)


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
    # Whether this job's engine can stop in a way that can be continued. Sent
    # rather than inferred in the browser, which knows nothing about engines —
    # and offering Pause on wget would mean a button that only ever 409s.
    can_pause: bool = False


class JobsClear(BaseModel):
    """Which finished jobs to delete. Everything is optional; nothing set
    means every job that is not queued or running."""

    model_config = ConfigDict(str_strip_whitespace=True)

    status: str | None = Field(default=None, max_length=16)
    type: str | None = Field(default=None, max_length=32)
    site_id: int | None = None


class JobsCleared(BaseModel):
    deleted: int


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


# ── feeds ────────────────────────────────────────────────────────────────


class FeedCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    url: str = Field(min_length=1, max_length=2048)
    kind: str = Field(default="auto", pattern="^(auto|rss|atom|json|sitemap|page)$")
    title: str | None = Field(default=None, max_length=255)
    interval_min: int | None = Field(default=None, ge=5, le=60 * 24 * 30)
    enabled: bool = True
    auto_capture: bool = True


class FeedUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str | None = Field(default=None, max_length=255)
    interval_min: int | None = Field(default=None, ge=5, le=60 * 24 * 30)
    enabled: bool | None = None
    auto_capture: bool | None = None
    recapture_on_update: bool | None = None


class FeedSummary(BaseModel):
    id: int
    site_id: int
    url: str
    kind: str
    title: str | None
    enabled: bool
    auto_capture: bool
    recapture_on_update: bool
    interval_min: int
    next_poll_at: datetime | None
    last_polled_at: datetime | None
    last_success_at: datetime | None
    last_status: int | None
    consecutive_failures: int
    last_error: str | None
    disabled_reason: str | None
    counts: dict[str, int]
    # The capture half's backoff. Exposed because a feed with pending items
    # that is not capturing them looks identical to a broken one otherwise —
    # which is the question that uncovered the runaway dispatch in the first
    # place.
    capture_failures: int = 0
    next_capture_at: datetime | None = None


class FeedPollEntry(BaseModel):
    id: int
    ts: datetime
    status: int
    duration_ms: int
    entries_seen: int
    new_items: int
    gone_items: int
    action: str
    error: str | None


class FeedItemEntry(BaseModel):
    id: int
    url: str
    title: str | None
    status: str
    published_at: datetime | None
    first_seen_at: datetime
    gone_at: datetime | None
    capture_id: int | None


class FeedCandidateModel(BaseModel):
    """What the add-feed dialog shows before anything is saved.

    `in_scope` and `out_of_scope` are the point of testing first: a feed whose
    entries fall outside the site's scope polls happily forever and archives
    nothing, which is a confusing failure to diagnose after the fact.
    """

    url: str
    kind: str
    title: str | None
    entry_count: int
    recent_titles: list[str]
    is_comments: bool
    in_scope: int
    out_of_scope: list[str]
    error: str | None
    ok: bool


class FeedTestRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    url: str = Field(min_length=1, max_length=2048)
    kind: str = Field(default="auto", pattern="^(auto|rss|atom|json|sitemap|page)$")


class FeedPollResult(BaseModel):
    status: int
    action: str
    entries_seen: int
    new_items: int
    gone_items: int
    baseline: bool
    error: str | None
    job_ids: list[int] = Field(default_factory=list)


# ── scheduling & notifications ───────────────────────────────────────────


class QuietHours(BaseModel):
    enabled: bool = False
    start: str = Field(default="01:00", pattern=r"^\d{1,2}:\d{2}$")
    end: str = Field(default="07:00", pattern=r"^\d{1,2}:\d{2}$")


class ScheduleSettings(BaseModel):
    quiet_hours: QuietHours
    per_host_serial: bool
    full_recapture_days: int = Field(ge=0, le=3650)
    in_quiet_hours_now: bool
    # How often the periodic report goes out. 0 switches it off; it is still
    # readable on demand, because that is where it is most often read.
    digest_every_days: int = Field(default=7, ge=0, le=365)


class NotifyTarget(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    url: str = Field(min_length=1, max_length=2048)
    enabled: bool = True
    label: str = Field(default="", max_length=80)


class NotifySettings(BaseModel):
    targets: list[NotifyTarget]
    events: dict[str, bool]
    labels: dict[str, str]
    apprise_available: bool


class NotifyUpdate(BaseModel):
    targets: list[NotifyTarget] | None = Field(default=None, max_length=20)
    events: dict[str, bool] | None = None


# ── engines ──────────────────────────────────────────────────────────────


class EngineSummary(BaseModel):
    """An installed engine, and whether it can actually run right now.

    `enabled` is about the manifest loading; `available` is about the
    environment. An engine that needs the Docker socket on a host without one
    is perfectly valid and completely unusable, and the picker has to be able
    to say which — otherwise it is selectable and fails at capture time.
    """

    id: str
    name: str
    version: str
    source: str
    description: str
    capabilities: dict[str, Any]
    enabled: bool
    available: bool = True
    unavailable_reason: str | None = None
    error: str | None = None


# ── search ───────────────────────────────────────────────────────────────


class SearchHit(BaseModel):
    site_id: int
    site_title: str
    site_slug: str
    folder_path: str
    url: str
    title: str
    snippets: list[str]
    score: float
    capture_id: int | None
    #: 14-digit CDXJ timestamp, so the UI can open replay at this version.
    timestamp: str
    words: int


class SearchResults(BaseModel):
    query: str
    terms: list[str]
    total: int
    hits: list[SearchHit]
    #: The candidate window filled up, so `total` is a floor rather than a count.
    truncated: bool = False


class SearchStatus(BaseModel):
    pages: int
    words: int
    sites: int
    #: Sites with captures and nothing indexed — the prompt to reindex.
    unindexed_sites: list[int]


# ── exports ──────────────────────────────────────────────────────────────


class ThumbnailSettings(BaseModel):
    enabled: bool


class ExportEntry(BaseModel):
    name: str
    size_bytes: int
    created_at: str


# ── retention ────────────────────────────────────────────────────────────


class RetentionPolicy(BaseModel):
    """A site's own retention rules. Every field is optional; what is not set
    falls back to the instance default, and that to `DEFAULT_POLICY`."""

    enabled: bool | None = None
    keep_last: int | None = Field(default=None, ge=0, le=1000)
    keep_monthly: int | None = Field(default=None, ge=0, le=600)
    min_age_days: int | None = Field(default=None, ge=0, le=3650)


class MediaPolicy(BaseModel):
    """A site's embedded-media rules, all optional and layered like retention.

    The ceilings are generous rather than tight — this is somebody's own NAS
    and the point of the feature is to keep video that is about to disappear.
    They exist because these three numbers are the only thing standing between
    an unattended nightly capture and a full disk, so "no limit" is not one of
    the things that can be typed here.
    """

    enabled: bool | None = None
    max_item_bytes: int | None = Field(default=None, ge=0, le=64 * 1024**3)
    max_total_bytes: int | None = Field(default=None, ge=0, le=512 * 1024**3)
    max_items: int | None = Field(default=None, ge=0, le=1000)
    # Reaches yt-dlp as a format selector, never a shell string. Bounded only
    # in length; yt-dlp rejects a malformed one and the failure is reported
    # per item, which is a better error than anything guessed at here.
    format: str | None = Field(default=None, min_length=1, max_length=200)
    allow_private_hosts: bool | None = None


class MetricsSettings(BaseModel):
    """The token is write-only. Reads report whether one is set, never what."""

    enabled: bool | None = None
    token: str | None = Field(default=None, max_length=256)
