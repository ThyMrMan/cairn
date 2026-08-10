"""Session cookie handling.

`Domain` is deliberately never set. A host-only cookie is not sent to any
other host, which is what keeps the replay origin — where archived
JavaScript executes — from ever seeing it (docs/11).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import Response

from cairn.config import Settings
from cairn.db.types import utcnow


def set_session_cookie(
    response: Response, settings: Settings, token: str, expires_at: datetime
) -> None:
    max_age = max(0, int((expires_at - utcnow()).total_seconds()))
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=settings.use_secure_cookies,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=settings.cookie_name,
        httponly=True,
        secure=settings.use_secure_cookies,
        samesite="lax",
        path="/",
    )
