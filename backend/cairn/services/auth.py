"""Authentication: sessions, rate limiting, TOTP.

Single user, but "single user" means there is exactly one credential between
the internet and everything — not that weak auth is acceptable (docs/11).
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

import pyotp
from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.crypto.passwords import hash_password, needs_rehash, verify_password
from cairn.crypto.sealing import Sealer
from cairn.db.models import LoginAttempt, Session_, User
from cairn.db.types import utcnow
from cairn.logging import get_logger
from cairn.services import audit

log = get_logger(__name__)

TOTP_CONTEXT = "user.totp"
RECOVERY_CONTEXT = "user.recovery"
SESSION_TOKEN_BYTES = 32  # 256 bits
RECOVERY_CODE_COUNT = 10
ATTEMPT_RETENTION_FACTOR = 8  # prune login_attempts older than window * this

# One message for every failure cause. Never distinguish "no such user" from
# "wrong password" from "wrong TOTP" — that is account enumeration.
GENERIC_FAILURE = "Invalid username or password."


class AuthError(Exception):
    """Login refused. `message` is safe to show the user verbatim."""

    def __init__(self, message: str = GENERIC_FAILURE, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


class TotpRequired(Exception):  # noqa: N818 — a control-flow signal, not a failure
    """Password was correct but a second factor is needed."""


@dataclass(slots=True)
class LoginResult:
    user: User
    token: str
    expires_at: datetime


# ── session tokens ───────────────────────────────────────────────────────


def session_id_for_token(token: str) -> str:
    """Sessions are stored hashed so a database read yields nothing usable.

    Plain SHA-256 is correct here, unlike for passwords: the token is 256 bits
    of CSPRNG output, so there is no dictionary to attack and no need for a
    slow KDF.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_hash_token = session_id_for_token  # internal alias, kept for readability below


def create_session(
    session: Session,
    user: User,
    settings: Settings,
    *,
    user_agent: str | None = None,
    ip: str | None = None,
) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    now = utcnow()
    expires_at = now + timedelta(days=settings.session_absolute_days)

    session.add(
        Session_(
            id=_hash_token(token),
            user_id=user.id,
            created_at=now,
            last_seen_at=now,
            expires_at=expires_at,
            user_agent=(user_agent or "")[:512] or None,
            ip=ip,
        )
    )
    return token, expires_at


def resolve_session(session: Session, token: str, settings: Settings) -> User | None:
    """Return the session's user, or None if it is invalid or expired.

    Enforces both an absolute lifetime and an idle timeout, and slides
    `last_seen_at` forward on use.
    """
    if not token:
        return None

    row = session.get(Session_, _hash_token(token))
    if row is None or row.revoked_at is not None:
        return None

    now = utcnow()
    if row.expires_at <= now:
        return None
    if now - row.last_seen_at > timedelta(days=settings.session_idle_days):
        row.revoked_at = now
        return None

    # Avoid a write on every single request; a minute of drift is harmless.
    if now - row.last_seen_at > timedelta(minutes=1):
        row.last_seen_at = now

    return session.get(User, row.user_id)


def revoke_session(session: Session, token: str) -> bool:
    row = session.get(Session_, _hash_token(token))
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = utcnow()
    return True


def revoke_session_by_id(session: Session, session_id: str, user_id: int) -> bool:
    row = session.get(Session_, session_id)
    if row is None or row.user_id != user_id or row.revoked_at is not None:
        return False
    row.revoked_at = utcnow()
    return True


def revoke_all_sessions(session: Session, user_id: int, *, except_token: str | None = None) -> int:
    keep = _hash_token(except_token) if except_token else None
    now = utcnow()
    rows = session.scalars(
        select(Session_).where(Session_.user_id == user_id, Session_.revoked_at.is_(None))
    ).all()
    count = 0
    for row in rows:
        if keep is not None and row.id == keep:
            continue
        row.revoked_at = now
        count += 1
    return count


def active_sessions(session: Session, user_id: int) -> list[Session_]:
    now = utcnow()
    stmt = (
        select(Session_)
        .where(
            Session_.user_id == user_id,
            Session_.revoked_at.is_(None),
            Session_.expires_at > now,
        )
        .order_by(Session_.last_seen_at.desc())
    )
    return list(session.scalars(stmt).all())


def purge_expired_sessions(session: Session) -> int:
    now = utcnow()
    # DELETE always yields a CursorResult; the generic Result type has no
    # rowcount, so the cast is what tells mypy which one this is.
    result = cast(
        CursorResult[Any],
        session.execute(
            delete(Session_).where(
                (Session_.expires_at <= now)
                | (
                    Session_.revoked_at.isnot(None)
                    & (Session_.revoked_at < now - timedelta(days=30))
                )
            )
        ),
    )
    return result.rowcount or 0


# ── rate limiting ────────────────────────────────────────────────────────


def _recent_failures(session: Session, settings: Settings, *, ip: str, username: str) -> int:
    cutoff = utcnow() - timedelta(seconds=settings.login_window_seconds)
    stmt = select(func.count(LoginAttempt.id)).where(
        LoginAttempt.ts >= cutoff,
        LoginAttempt.successful.is_(False),
    )
    if ip and username:
        stmt = stmt.where((LoginAttempt.ip == ip) | (LoginAttempt.username == username))
    elif ip:
        stmt = stmt.where(LoginAttempt.ip == ip)
    else:
        stmt = stmt.where(LoginAttempt.username == username)
    return session.scalar(stmt) or 0


def _lockout_seconds(settings: Settings, failure_count: int) -> int:
    """Progressive backoff: 1m, 2m, 4m, … capped at the configured maximum."""
    over = max(0, failure_count - settings.login_max_attempts)
    return int(min(settings.login_lockout_seconds, 60 * (2**over)))


def record_attempt(session: Session, *, ip: str, username: str, successful: bool) -> None:
    session.add(
        LoginAttempt(
            ts=utcnow(), ip=ip or "", username=(username or "")[:64], successful=successful
        )
    )


def prune_attempts(session: Session, settings: Settings) -> int:
    cutoff = utcnow() - timedelta(seconds=settings.login_window_seconds * ATTEMPT_RETENTION_FACTOR)
    result = cast(
        CursorResult[Any], session.execute(delete(LoginAttempt).where(LoginAttempt.ts < cutoff))
    )
    return result.rowcount or 0


def check_rate_limit(session: Session, settings: Settings, *, ip: str, username: str) -> None:
    """Raise AuthError with a retry_after when this caller is throttled.

    Limits apply per IP *and* per account: per-IP alone lets a botnet spread
    attempts across addresses, per-account alone lets one IP enumerate many
    accounts. Both matter even with a single user.
    """
    failures = _recent_failures(session, settings, ip=ip, username=username)
    if failures >= settings.login_max_attempts:
        retry_after = _lockout_seconds(settings, failures)
        raise AuthError(
            f"Too many failed attempts. Try again in {retry_after // 60 or 1} minute(s).",
            retry_after=retry_after,
        )


# ── TOTP ─────────────────────────────────────────────────────────────────


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, username: str, issuer: str = "Cairn") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def verify_totp(secret: str, code: str) -> bool:
    # valid_window=1 accepts the adjacent 30s step, covering clock skew
    # between the NAS and the phone — a real problem on a device whose NTP
    # may be blocked.
    return pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    return [
        f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}"
        for _ in range(count)
    ]


def consume_recovery_code(user: User, sealer: Sealer, code: str) -> bool:
    """Spend a one-time recovery code. Returns True if it matched."""
    if not user.recovery_codes:
        return False
    raw = sealer.unseal_text(user.recovery_codes, context=RECOVERY_CONTEXT)
    remaining = [c for c in raw.split("\n") if c]
    normalized = code.strip().lower()
    # Compare against every code regardless of an early match, so timing does
    # not reveal the position of the matching code.
    matched = False
    kept: list[str] = []
    for candidate in remaining:
        if secrets.compare_digest(candidate, normalized) and not matched:
            matched = True
        else:
            kept.append(candidate)
    if matched:
        user.recovery_codes = sealer.seal("\n".join(kept), context=RECOVERY_CONTEXT)
    return matched


# ── login ────────────────────────────────────────────────────────────────


def get_user(session: Session, username: str) -> User | None:
    return session.scalar(select(User).where(User.username == username))


def user_count(session: Session) -> int:
    return session.scalar(select(func.count(User.id))) or 0


def authenticate(
    session: Session,
    settings: Settings,
    sealer: Sealer,
    *,
    username: str,
    password: str,
    totp_code: str | None = None,
    user_agent: str | None = None,
    ip: str = "",
) -> LoginResult:
    """Verify credentials and open a session.

    Raises AuthError on any failure, always with the same message.
    Raises TotpRequired when the password is right but a code is needed —
    that distinction is only reachable *after* a correct password, so it
    leaks nothing to an unauthenticated attacker.
    """
    username = (username or "").strip()

    try:
        check_rate_limit(session, settings, ip=ip, username=username)
    except AuthError:
        audit.record(session, audit.LOGIN_RATE_LIMITED, actor=username or None, ip=ip)
        raise

    user = get_user(session, username)

    # Runs the hash comparison even when the user does not exist, so the
    # timing profile is identical either way.
    password_ok = verify_password(user.password_hash if user else None, password)

    if user is not None and user.locked_until is not None and user.locked_until > utcnow():
        record_attempt(session, ip=ip, username=username, successful=False)
        audit.record(session, audit.LOGIN_LOCKED, actor=username, ip=ip)
        remaining = int((user.locked_until - utcnow()).total_seconds())
        raise AuthError(
            f"Account temporarily locked. Try again in {remaining // 60 or 1} minute(s).",
            retry_after=remaining,
        )

    if user is None or not password_ok:
        _register_failure(session, settings, user, username=username, ip=ip)
        raise AuthError()

    if user.totp_enabled:
        if not totp_code:
            raise TotpRequired()
        secret = sealer.unseal_text(user.totp_secret or b"", context=TOTP_CONTEXT)
        if not verify_totp(secret, totp_code):
            if consume_recovery_code(user, sealer, totp_code):
                audit.record(session, audit.TOTP_RECOVERY_USED, actor=username, ip=ip)
            else:
                _register_failure(session, settings, user, username=username, ip=ip)
                raise AuthError()

    # Success — clear the failure state and refresh the hash if the cost
    # parameters have been raised since this password was set.
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    record_attempt(session, ip=ip, username=username, successful=True)
    token, expires_at = create_session(session, user, settings, user_agent=user_agent, ip=ip)
    audit.record(session, audit.LOGIN_OK, actor=username, ip=ip)
    prune_attempts(session, settings)

    return LoginResult(user=user, token=token, expires_at=expires_at)


def _register_failure(
    session: Session, settings: Settings, user: User | None, *, username: str, ip: str
) -> None:
    record_attempt(session, ip=ip, username=username, successful=False)
    if user is not None:
        user.failed_logins += 1
        if user.failed_logins >= settings.login_max_attempts:
            user.locked_until = utcnow() + timedelta(
                seconds=_lockout_seconds(settings, user.failed_logins)
            )
    audit.record(session, audit.LOGIN_FAIL, actor=username or None, ip=ip)
    log.info("failed login", extra={"username": username, "ip": ip})


def change_password(
    session: Session,
    user: User,
    *,
    current: str,
    new: str,
    keep_token: str | None = None,
) -> int:
    """Change the password and revoke every other session.

    Returns the number of sessions revoked. Revoking on password change is
    the point of changing it after a suspected compromise.
    """
    if not verify_password(user.password_hash, current):
        raise AuthError("Current password is incorrect.")
    user.password_hash = hash_password(new)
    revoked = revoke_all_sessions(session, user.id, except_token=keep_token)
    audit.record(session, audit.PASSWORD_CHANGED, actor=user.username, detail={"revoked": revoked})
    return revoked
