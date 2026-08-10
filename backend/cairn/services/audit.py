"""Audit log.

Every authentication event and privileged action gets a row. `detail` is
JSON and must never contain secret values — cookie jars, passwords, TOTP
secrets, session tokens (docs/11).
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.orm import Session

from cairn.db.models import AuditLog
from cairn.db.types import utcnow

# Actions are a closed vocabulary so the log stays greppable.
LOGIN_OK = "login.ok"
LOGIN_FAIL = "login.fail"
LOGIN_LOCKED = "login.locked"
LOGIN_RATE_LIMITED = "login.rate_limited"
LOGOUT = "logout"
SETUP_COMPLETE = "setup.complete"
PASSWORD_CHANGED = "password.changed"  # noqa: S105 — an action name, not a credential
TOTP_ENABLED = "totp.enabled"
TOTP_DISABLED = "totp.disabled"
TOTP_RECOVERY_USED = "totp.recovery_used"
ACCOUNT_UNLOCKED = "account.unlocked"
SESSION_REVOKED = "session.revoked"
SESSIONS_REVOKED_ALL = "session.revoked_all"

_FORBIDDEN_DETAIL_KEYS = frozenset(
    {"password", "token", "secret", "cookies", "totp_secret", "recovery_codes", "session"}
)


def record(
    session: Session,
    action: str,
    *,
    actor: str | None = None,
    target: str | None = None,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> AuditLog:
    if detail:
        leaked = _FORBIDDEN_DETAIL_KEYS.intersection(detail)
        if leaked:
            # A programming error, not a runtime condition — fail loudly in
            # tests rather than quietly writing a credential to the log.
            raise ValueError(f"audit detail must not contain secret keys: {sorted(leaked)}")

    entry = AuditLog(
        ts=utcnow(),
        actor=actor,
        action=action,
        target=target,
        detail=detail,
        ip=ip,
    )
    session.add(entry)
    return entry


def recent(session: Session, *, limit: int = 100, offset: int = 0) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.ts.desc(), AuditLog.id.desc())
    return list(session.scalars(stmt.limit(limit).offset(offset)).all())


def prune(session: Session, *, keep_days: int = 365) -> int:
    from datetime import timedelta

    cutoff = utcnow() - timedelta(days=keep_days)
    # DELETE always yields a CursorResult; the generic Result type has no
    # rowcount, so the cast is what tells mypy which one this is.
    result = cast(CursorResult[Any], session.execute(delete(AuditLog).where(AuditLog.ts < cutoff)))
    return result.rowcount or 0
