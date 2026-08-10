"""SQLAlchemy models — the full schema from docs/03.

M0 only exercises users/sessions/audit_log/settings, but the whole schema
ships in the first migration. Adding tables later is easy; discovering that
the shape was wrong after there is data in them is not.

The database is an index over the filesystem, not the system of record. Every
site directory carries a site.yaml and every capture a manifest.json, so a
rebuild job can reconstruct these rows from disk (docs/03).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cairn.db.base import Base
from cairn.db.types import JsonText, UtcDateTime, utcnow

# ── status vocabularies ──────────────────────────────────────────────────
# Kept as plain strings rather than SQL enums: SQLite has no ENUM type, and
# CHECK constraints on status columns make every future state addition a
# migration. Validation lives in the service layer.

SITE_STATUSES = ("new", "indexed", "ready", "capturing", "error", "archived")
JOB_STATUSES = ("queued", "running", "ok", "failed", "cancelled", "interrupted")
JOB_TYPES = ("discovery", "capture", "mint", "index", "export", "move", "verify", "purge")
CAPTURE_STATUSES = ("running", "ok", "partial", "failed", "cancelled", "interrupted")
CAPTURE_KINDS = ("full", "incremental", "feed", "manual", "resume")
PROFILE_MODES = ("none", "cookies", "userscript", "interactive")
FEED_KINDS = ("auto", "rss", "atom", "sitemap", "json")


# ── identity & auth ──────────────────────────────────────────────────────


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    totp_secret: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    totp_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recovery_codes: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    sessions: Mapped[list[Session_]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def totp_enabled(self) -> bool:
        return self.totp_secret is not None and self.totp_confirmed


class Session_(Base):  # noqa: N801 — trailing underscore avoids clashing with orm.Session
    """A logged-in session.

    `id` holds the *hash* of the token, never the token itself — a database
    read must not yield usable sessions (docs/11).
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(512), default=None)
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    user: Mapped[User] = relationship(back_populates="sessions")

    __table_args__ = (Index("ix_sessions_user", "user_id"),)


class LoginAttempt(Base):
    """Rate-limiting ledger, keyed by IP and by username.

    Separate from audit_log because it is high-churn and gets pruned
    aggressively, while the audit log is kept.
    """

    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    username: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    successful: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_login_attempts_ip_ts", "ip", "ts"),
        Index("ix_login_attempts_user_ts", "username", "ts"),
    )


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    actor: Mapped[str | None] = mapped_column(String(64), default=None)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str | None] = mapped_column(String(255), default=None)
    detail: Mapped[Any | None] = mapped_column(JsonText, default=None)
    ip: Mapped[str | None] = mapped_column(String(64), default=None)

    __table_args__ = (Index("ix_audit_ts", text("ts DESC")),)


# ── organization ─────────────────────────────────────────────────────────


class Folder(Base):
    __tablename__ = "folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("folders.id", ondelete="RESTRICT"), default=None
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    # Materialized path, e.g. "Blogs/Photography". Read constantly, rewritten
    # rarely; a rename is one UPDATE ... WHERE path LIKE 'old/%'.
    path: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    parent: Mapped[Folder | None] = relationship(remote_side="Folder.id", back_populates="children")
    children: Mapped[list[Folder]] = relationship(back_populates="parent")
    sites: Mapped[list[Site]] = relationship(back_populates="folder")

    __table_args__ = (UniqueConstraint("parent_id", "slug", name="uq_folders_parent_slug"),)


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    color: Mapped[str | None] = mapped_column(String(16), default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)


class SiteTag(Base):
    __tablename__ = "site_tags"

    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)


# ── sites ────────────────────────────────────────────────────────────────


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    folder_id: Mapped[int] = mapped_column(
        ForeignKey("folders.id", ondelete="RESTRICT"), nullable=False
    )
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    seed_url: Mapped[str] = mapped_column(Text, nullable=False)
    primary_host: Mapped[str] = mapped_column(String(255), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("access_profiles.id", ondelete="SET NULL"), default=None
    )
    engine_id: Mapped[str] = mapped_column(String(64), nullable=False, default="wget-warc")
    # Validated against the engine's own JSON Schema, which declares
    # additionalProperties: false — so nothing but engine knobs may live here.
    # Site-level crawl settings go in scope_settings.
    engine_config: Mapped[Any] = mapped_column(JsonText, nullable=False, default=dict)
    # Scope decisions that are not per-host: limits, robots, politeness.
    scope_settings: Mapped[Any] = mapped_column(JsonText, nullable=False, default=dict)
    keep_mirror: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="new")
    archive_path: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    url_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_capture_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    folder: Mapped[Folder] = relationship(back_populates="sites")
    profile: Mapped[AccessProfile | None] = relationship(back_populates="sites")
    captures: Mapped[list[Capture]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )
    feeds: Mapped[list[Feed]] = relationship(back_populates="site", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("folder_id", "slug", name="uq_sites_folder_slug"),
        Index("ix_sites_folder", "folder_id"),
        Index("ix_sites_host", "primary_host"),
    )


# ── discovery & scope ────────────────────────────────────────────────────


class Discovery(Base):
    __tablename__ = "discoveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), default=None
    )
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    pages_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    urls_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary: Mapped[Any | None] = mapped_column(JsonText, default=None)

    hosts: Mapped[list[DiscoveredHost]] = relationship(
        back_populates="discovery", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_discoveries_site", "site_id"),)


class DiscoveredHost(Base):
    __tablename__ = "discovered_hosts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    discovery_id: Mapped[int] = mapped_column(
        ForeignKey("discoveries.id", ondelete="CASCADE"), nullable=False
    )
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    registrable: Mapped[str] = mapped_column(String(255), nullable=False)
    is_seed_host: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    link_refs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    asset_refs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    distinct_urls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    role_guess: Mapped[str | None] = mapped_column(String(32), default=None)
    sample_urls: Mapped[Any | None] = mapped_column(JsonText, default=None)

    discovery: Mapped[Discovery] = relationship(back_populates="hosts")

    __table_args__ = (
        UniqueConstraint("discovery_id", "host", name="uq_discovered_hosts_discovery_host"),
    )


class ScopeRule(Base):
    """Per-host scope decision.

    `crawl_pages` and `fetch_assets` are deliberately independent: you want
    1.bp.blogspot.com's images without crawling it as a website. Collapsing
    these into one flag is the most likely early mistake (docs/03).
    """

    __tablename__ = "scope_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    crawl_pages: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fetch_assets: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    path_prefix: Mapped[str | None] = mapped_column(String(1024), default=None)
    # Permit extension-less URLs on an assets-only host. No regex over URLs can
    # tell an extension-less image from an extension-less page, so this is an
    # explicit per-host decision rather than something inferred (docs/04).
    allow_extensionless: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (UniqueConstraint("site_id", "host", name="uq_scope_rules_site_host"),)


class ScopePattern(Base):
    __tablename__ = "scope_patterns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # accept | reject
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, default=None)

    __table_args__ = (Index("ix_scope_patterns_site", "site_id"),)


# ── captures ─────────────────────────────────────────────────────────────


class Capture(Base):
    __tablename__ = "captures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), default=None
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    engine_id: Mapped[str] = mapped_column(String(64), nullable=False)
    engine_version: Mapped[str | None] = mapped_column(String(64), default=None)
    dir_name: Mapped[str] = mapped_column(String(255), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    url_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bytes_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warc_files: Mapped[Any | None] = mapped_column(JsonText, default=None)
    indexed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    site: Mapped[Site] = relationship(back_populates="captures")

    __table_args__ = (
        UniqueConstraint("site_id", "dir_name", name="uq_captures_site_dir"),
        Index("ix_captures_site", "site_id"),
    )


class CaptureUrl(Base):
    """One row per fetched URL.

    Highest-volume table by far. Insert in batched transactions, never per
    event, and prune for superseded captures — the CDXJ index remains the
    authoritative URL list (docs/03).
    """

    __tablename__ = "capture_urls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    capture_id: Mapped[int] = mapped_column(
        ForeignKey("captures.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, default=None)
    mime: Mapped[str | None] = mapped_column(String(128), default=None)
    size_bytes: Mapped[int | None] = mapped_column(Integer, default=None)
    digest: Mapped[str | None] = mapped_column(String(64), default=None)
    is_revisit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fetched_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)

    __table_args__ = (
        Index("ix_curls_capture", "capture_id"),
        Index("ix_curls_url", "url"),
        Index(
            "ix_curls_errors",
            "capture_id",
            sqlite_where=text("status_code >= 400 OR error IS NOT NULL"),
        ),
    )


# ── feeds ────────────────────────────────────────────────────────────────


class Feed(Base):
    __tablename__ = "feeds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    title: Mapped[str | None] = mapped_column(String(255), default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    interval_min: Mapped[int] = mapped_column(Integer, nullable=False, default=360)
    last_polled_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    last_success_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    etag: Mapped[str | None] = mapped_column(String(255), default=None)
    last_modified: Mapped[str | None] = mapped_column(String(64), default=None)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)

    site: Mapped[Site] = relationship(back_populates="feeds")
    items: Mapped[list[FeedItem]] = relationship(
        back_populates="feed", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("site_id", "url", name="uq_feeds_site_url"),)


class FeedItem(Base):
    __tablename__ = "feed_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_id: Mapped[int] = mapped_column(ForeignKey("feeds.id", ondelete="CASCADE"), nullable=False)
    guid: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, default=None)
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    first_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    capture_id: Mapped[int | None] = mapped_column(
        ForeignKey("captures.id", ondelete="SET NULL"), default=None
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")

    feed: Mapped[Feed] = relationship(back_populates="items")

    __table_args__ = (UniqueConstraint("feed_id", "guid", name="uq_feed_items_feed_guid"),)


# ── access profiles ──────────────────────────────────────────────────────


class AccessProfile(Base):
    """Reusable authentication material.

    No column here is ever serialized to an API response. Reads return
    metadata only: name, mode, hosts, expiry, fingerprint (docs/06).
    """

    __tablename__ = "access_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    user_agent: Mapped[str | None] = mapped_column(String(512), default=None)
    hosts: Mapped[Any | None] = mapped_column(JsonText, default=None)
    cookies_enc: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    script_enc: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    cookie_meta: Mapped[Any | None] = mapped_column(JsonText, default=None)
    minted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    fingerprint: Mapped[str | None] = mapped_column(String(80), default=None)
    last_verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    last_verify_result: Mapped[str | None] = mapped_column(String(32), default=None)
    verify_url: Mapped[str | None] = mapped_column(Text, default=None)
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    sites: Mapped[list[Site]] = relationship(back_populates="profile")


# ── jobs & engines ───────────────────────────────────────────────────────


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    site_id: Mapped[int | None] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), default=None
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    spec: Mapped[Any] = mapped_column(JsonText, nullable=False, default=dict)
    progress: Mapped[Any | None] = mapped_column(JsonText, default=None)
    pid: Mapped[int | None] = mapped_column(Integer, default=None)
    queued_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (Index("ix_jobs_queue", "status", "priority", "queued_at"),)


class EngineRecord(Base):
    __tablename__ = "engines"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # builtin|dropin|docker
    manifest: Mapped[Any] = mapped_column(JsonText, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    installed_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)


class Setting(Base):
    """Runtime settings — anything changeable without a container restart."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JsonText, nullable=False)


class SavedView(Base):
    __tablename__ = "saved_views"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    query: Mapped[Any] = mapped_column(JsonText, nullable=False, default=dict)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


__all__ = [
    "AccessProfile",
    "AuditLog",
    "Capture",
    "CaptureUrl",
    "DiscoveredHost",
    "Discovery",
    "EngineRecord",
    "Feed",
    "FeedItem",
    "Folder",
    "Job",
    "LoginAttempt",
    "SavedView",
    "ScopePattern",
    "ScopeRule",
    "Session_",
    "Setting",
    "Site",
    "SiteTag",
    "Tag",
    "User",
]
