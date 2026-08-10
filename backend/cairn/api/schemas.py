"""Request and response models.

Response models are explicit rather than serialized ORM objects: a model
gains a `cookies_enc` column one day and an implicit serializer publishes it.
"""

from __future__ import annotations

from datetime import datetime

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
