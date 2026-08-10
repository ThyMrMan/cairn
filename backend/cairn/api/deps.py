"""FastAPI dependencies."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request, status
from sqlalchemy.orm import Session

from cairn.api.errors import ApiError
from cairn.config import Settings
from cairn.crypto.sealing import Sealer
from cairn.db.models import User
from cairn.services import auth

# Methods that change state and therefore need CSRF protection.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "XMLHttpRequest"


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_sealer(request: Request) -> Sealer:
    sealer: Sealer = request.app.state.sealer
    return sealer


def get_db(request: Request) -> Iterator[Session]:
    """Request-scoped session. Commits on success, rolls back on error.

    Routes that must persist work while still returning an error status —
    failed logins writing to the rate-limit ledger, for instance — commit
    explicitly before raising.
    """
    factory = request.app.state.sessionmaker
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _proxy_is_trusted(request: Request, settings: Settings) -> bool:
    if not settings.trusted_proxy or request.client is None:
        return False
    try:
        peer = ipaddress.ip_address(request.client.host)
    except ValueError:
        return False
    for entry in settings.trusted_proxy.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                if peer in ipaddress.ip_network(entry, strict=False):
                    return True
            elif peer == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def client_ip(request: Request) -> str:
    """The caller's address, honouring X-Forwarded-For only behind a trusted proxy.

    Trusting the header unconditionally lets anyone spoof their address past
    the login rate limiter, which is the whole point of the limiter (docs/10).
    """
    settings: Settings = request.app.state.settings
    if _proxy_is_trusted(request, settings):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def require_csrf(request: Request) -> None:
    """Require a header that a cross-site form cannot set.

    SameSite=Lax already blocks form-based CSRF, but it is a single point of
    failure and browsers have shipped bugs. A custom header cannot be added
    cross-origin without a preflight, which we never grant (docs/11).
    """
    if request.method not in MUTATING_METHODS:
        return
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise ApiError(
            "csrf_failed",
            f"Missing or invalid {CSRF_HEADER} header.",
            status_code=status.HTTP_403_FORBIDDEN,
        )


def get_session_token(request: Request) -> str:
    settings: Settings = request.app.state.settings
    return request.cookies.get(settings.cookie_name, "")


def current_user_optional(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> User | None:
    # Trusted-header auth (Authelia / Authentik / Cloudflare Access). Only
    # honoured from a trusted proxy; config validation refuses to enable it
    # without one, and this is the second gate.
    if settings.auth_header_mode and _proxy_is_trusted(request, settings):
        username = request.headers.get(settings.auth_header_name, "").strip()
        if username:
            return auth.get_user(db, username)

    token = request.cookies.get(settings.cookie_name, "")
    if not token:
        return None
    return auth.resolve_session(db, token, settings)


def current_user(
    user: Annotated[User | None, Depends(current_user_optional)],
) -> User:
    if user is None:
        raise ApiError(
            "unauthenticated",
            "Authentication required.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    return user


DbSession = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings_dep)]
AppSealer = Annotated[Sealer, Depends(get_sealer)]
CurrentUser = Annotated[User, Depends(current_user)]
OptionalUser = Annotated[User | None, Depends(current_user_optional)]
ClientIp = Annotated[str, Depends(client_ip)]
Csrf = Depends(require_csrf)
