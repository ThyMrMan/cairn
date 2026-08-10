"""First-run account creation."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from cairn.api.cookies import set_session_cookie
from cairn.api.deps import AppSettings, ClientIp, Csrf, DbSession
from cairn.api.errors import ApiError
from cairn.api.schemas import LoginResponse, SetupRequest, SetupStatus
from cairn.services import auth, setup

router = APIRouter(prefix="/setup", tags=["setup"])


@router.get("", response_model=SetupStatus)
def status_(db: DbSession, settings: AppSettings) -> SetupStatus:
    return SetupStatus(
        setup_complete=setup.is_complete(db),
        password_min_length=settings.password_min_length,
    )


@router.post("", response_model=LoginResponse, status_code=status.HTTP_201_CREATED)
def create_account(
    body: SetupRequest,
    request: Request,
    response: Response,
    db: DbSession,
    settings: AppSettings,
    ip: ClientIp,
    _: None = Csrf,
) -> LoginResponse:
    """Create the single user account and log them straight in.

    Guarded server-side: once any user exists this returns 409 forever,
    regardless of what the UI shows.
    """
    try:
        user = setup.create_first_user(
            db, settings, username=body.username, password=body.password, ip=ip
        )
    except setup.SetupError as exc:
        code = "setup_complete" if setup.is_complete(db) else "setup_invalid"
        raise ApiError(
            code,
            exc.message,
            status_code=status.HTTP_409_CONFLICT
            if code == "setup_complete"
            else status.HTTP_400_BAD_REQUEST,
            detail=exc.problems or None,
        ) from exc

    token, expires_at = auth.create_session(
        db, user, settings, user_agent=request.headers.get("User-Agent"), ip=ip
    )
    set_session_cookie(response, settings, token, expires_at)

    return LoginResponse(
        username=user.username, expires_at=expires_at, totp_enabled=user.totp_enabled
    )
