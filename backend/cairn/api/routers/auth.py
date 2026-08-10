"""Login, logout, password, TOTP, session management."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from cairn.api.cookies import clear_session_cookie, set_session_cookie
from cairn.api.deps import (
    AppSealer,
    AppSettings,
    ClientIp,
    Csrf,
    CurrentUser,
    DbSession,
    get_session_token,
)
from cairn.api.errors import ApiError
from cairn.api.schemas import (
    LoginRequest,
    LoginResponse,
    MeResponse,
    Ok,
    PasswordChangeRequest,
    PasswordChangeResponse,
    SessionInfo,
    TotpConfirmRequest,
    TotpConfirmResponse,
    TotpDisableRequest,
    TotpSetupResponse,
)
from cairn.crypto.passwords import validate_password_strength, verify_password
from cairn.services import audit, auth

router = APIRouter(prefix="/auth", tags=["auth"])

SessionToken = Annotated[str, Depends(get_session_token)]


@router.post("/login", response_model=LoginResponse)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: DbSession,
    settings: AppSettings,
    sealer: AppSealer,
    ip: ClientIp,
    _: None = Csrf,
) -> LoginResponse:
    try:
        result = auth.authenticate(
            db,
            settings,
            sealer,
            username=body.username,
            password=body.password,
            totp_code=body.totp,
            user_agent=request.headers.get("User-Agent"),
            ip=ip,
        )
    except auth.TotpRequired as exc:
        # Only reachable after a correct password, so revealing that a second
        # factor is required leaks nothing to an unauthenticated attacker.
        db.commit()
        raise ApiError(
            "totp_required",
            "Enter the code from your authenticator app.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        ) from exc
    except auth.AuthError as exc:
        # Commit before raising: the failed attempt and its audit row must
        # persist, or the rate limiter has nothing to count.
        db.commit()
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        raise ApiError(
            "rate_limited" if exc.retry_after else "invalid_credentials",
            exc.message,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS
            if exc.retry_after
            else status.HTTP_401_UNAUTHORIZED,
            headers=headers,
        ) from exc

    set_session_cookie(response, settings, result.token, result.expires_at)
    return LoginResponse(
        username=result.user.username,
        expires_at=result.expires_at,
        totp_enabled=result.user.totp_enabled,
    )


@router.post("/logout", response_model=Ok)
def logout(
    response: Response,
    db: DbSession,
    settings: AppSettings,
    token: SessionToken,
    ip: ClientIp,
    user: CurrentUser,
    _: None = Csrf,
) -> Ok:
    auth.revoke_session(db, token)
    audit.record(db, audit.LOGOUT, actor=user.username, ip=ip)
    clear_session_cookie(response, settings)
    return Ok()


@router.get("/me", response_model=MeResponse)
def me(user: CurrentUser) -> MeResponse:
    return MeResponse(
        username=user.username,
        totp_enabled=user.totp_enabled,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


@router.post("/password", response_model=PasswordChangeResponse)
def change_password(
    body: PasswordChangeRequest,
    db: DbSession,
    settings: AppSettings,
    user: CurrentUser,
    token: SessionToken,
    _: None = Csrf,
) -> PasswordChangeResponse:
    problems = validate_password_strength(body.new, settings.password_min_length)
    if problems:
        raise ApiError("password_weak", "That password is not acceptable.", detail=problems)

    try:
        revoked = auth.change_password(
            db, user, current=body.current, new=body.new, keep_token=token
        )
    except auth.AuthError as exc:
        raise ApiError(
            "invalid_credentials",
            exc.message,
            status_code=status.HTTP_401_UNAUTHORIZED,
        ) from exc

    return PasswordChangeResponse(revoked_sessions=revoked)


# ── TOTP ─────────────────────────────────────────────────────────────────


@router.post("/totp/setup", response_model=TotpSetupResponse)
def totp_setup(
    db: DbSession, sealer: AppSealer, user: CurrentUser, _: None = Csrf
) -> TotpSetupResponse:
    """Generate an unconfirmed secret.

    Stored immediately but with `totp_confirmed = False`, so a secret that is
    never confirmed can never lock the account out.
    """
    if user.totp_enabled:
        raise ApiError(
            "totp_already_enabled",
            "Two-factor authentication is already enabled.",
            status_code=status.HTTP_409_CONFLICT,
        )

    secret = auth.generate_totp_secret()
    user.totp_secret = sealer.seal(secret, context=auth.TOTP_CONTEXT)
    user.totp_confirmed = False

    return TotpSetupResponse(
        secret=secret,
        provisioning_uri=auth.totp_provisioning_uri(secret, user.username),
    )


@router.post("/totp/confirm", response_model=TotpConfirmResponse)
def totp_confirm(
    body: TotpConfirmRequest,
    db: DbSession,
    sealer: AppSealer,
    user: CurrentUser,
    ip: ClientIp,
    _: None = Csrf,
) -> TotpConfirmResponse:
    if user.totp_secret is None:
        raise ApiError("totp_not_started", "Start two-factor setup first.")
    if user.totp_confirmed:
        raise ApiError(
            "totp_already_enabled",
            "Two-factor authentication is already enabled.",
            status_code=status.HTTP_409_CONFLICT,
        )

    secret = sealer.unseal_text(user.totp_secret, context=auth.TOTP_CONTEXT)
    if not auth.verify_totp(secret, body.code):
        raise ApiError(
            "totp_invalid",
            "That code did not match. Check your device's clock is correct.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    codes = auth.generate_recovery_codes()
    user.recovery_codes = sealer.seal("\n".join(codes), context=auth.RECOVERY_CONTEXT)
    user.totp_confirmed = True
    audit.record(db, audit.TOTP_ENABLED, actor=user.username, ip=ip)

    # The only time these are ever returned. They are sealed from here on.
    return TotpConfirmResponse(recovery_codes=codes)


@router.delete("/totp", response_model=Ok)
def totp_disable(
    body: TotpDisableRequest,
    db: DbSession,
    sealer: AppSealer,
    user: CurrentUser,
    ip: ClientIp,
    _: None = Csrf,
) -> Ok:
    """Disable 2FA. Requires the password *and* a current code.

    Reauthentication on removal is the point: a stolen session should not be
    able to strip the second factor off the account.
    """
    if not user.totp_enabled:
        raise ApiError("totp_not_enabled", "Two-factor authentication is not enabled.")
    if not verify_password(user.password_hash, body.password):
        raise ApiError(
            "invalid_credentials",
            "Password is incorrect.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    secret = sealer.unseal_text(user.totp_secret or b"", context=auth.TOTP_CONTEXT)
    if not auth.verify_totp(secret, body.code) and not auth.consume_recovery_code(
        user, sealer, body.code
    ):
        raise ApiError(
            "totp_invalid", "That code did not match.", status_code=status.HTTP_401_UNAUTHORIZED
        )

    user.totp_secret = None
    user.totp_confirmed = False
    user.recovery_codes = None
    audit.record(db, audit.TOTP_DISABLED, actor=user.username, ip=ip)
    return Ok()


# ── sessions ─────────────────────────────────────────────────────────────


@router.get("/sessions", response_model=list[SessionInfo])
def list_sessions(db: DbSession, user: CurrentUser, token: SessionToken) -> list[SessionInfo]:
    current_id = auth.session_id_for_token(token) if token else ""
    return [
        SessionInfo(
            id=row.id,
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
            expires_at=row.expires_at,
            user_agent=row.user_agent,
            ip=row.ip,
            current=row.id == current_id,
        )
        for row in auth.active_sessions(db, user.id)
    ]


@router.delete("/sessions/{session_id}", response_model=Ok)
def revoke_session(
    session_id: str,
    db: DbSession,
    user: CurrentUser,
    ip: ClientIp,
    _: None = Csrf,
) -> Ok:
    if not auth.revoke_session_by_id(db, session_id, user.id):
        raise ApiError("not_found", "No such session.", status_code=status.HTTP_404_NOT_FOUND)
    audit.record(db, audit.SESSION_REVOKED, actor=user.username, target=session_id[:16], ip=ip)
    return Ok()


@router.delete("/sessions", response_model=Ok)
def revoke_other_sessions(
    db: DbSession,
    user: CurrentUser,
    token: SessionToken,
    ip: ClientIp,
    _: None = Csrf,
) -> Ok:
    count = auth.revoke_all_sessions(db, user.id, except_token=token)
    audit.record(
        db, audit.SESSIONS_REVOKED_ALL, actor=user.username, detail={"count": count}, ip=ip
    )
    return Ok()
