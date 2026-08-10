"""First-run setup.

The setup endpoint is reachable without authentication by necessity — there
is no account yet to authenticate against. It must therefore become
permanently unreachable the moment a user exists, checked server-side on
every call rather than hidden in the UI (docs/11).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.crypto.passwords import hash_password, validate_password_strength
from cairn.db.models import User
from cairn.db.types import utcnow
from cairn.services import audit
from cairn.services.auth import user_count

USERNAME_MIN = 3
USERNAME_MAX = 64


class SetupError(Exception):
    def __init__(self, message: str, *, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.problems = problems or []


def is_complete(session: Session) -> bool:
    return user_count(session) > 0


def validate_username(username: str) -> list[str]:
    problems: list[str] = []
    if not (USERNAME_MIN <= len(username) <= USERNAME_MAX):
        problems.append(f"Must be {USERNAME_MIN} to {USERNAME_MAX} characters.")
    if not username.replace("_", "").replace("-", "").replace(".", "").isalnum():
        problems.append("Letters, digits, dot, dash and underscore only.")
    return problems


def create_first_user(
    session: Session,
    settings: Settings,
    *,
    username: str,
    password: str,
    ip: str = "",
) -> User:
    if is_complete(session):
        raise SetupError("Setup has already been completed.")

    username = (username or "").strip()
    problems = validate_username(username)
    problems += validate_password_strength(password, settings.password_min_length)
    if problems:
        raise SetupError("Could not create the account.", problems=problems)

    user = User(
        username=username,
        password_hash=hash_password(password),
        created_at=utcnow(),
    )
    session.add(user)
    session.flush()

    audit.record(session, audit.SETUP_COMPLETE, actor=username, target=username, ip=ip)
    return user
