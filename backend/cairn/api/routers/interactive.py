"""The interactive profile session: a live browser, driven from the UI.

Split from the profiles router because of the WebSocket, which does not obey
any of the protections the rest of the API relies on and needs its own.

**A WebSocket is not covered by the CSRF header check.** `X-Requested-With`
cannot be set on a cross-origin `fetch` without a preflight, which is what
protects every other mutating route. The WebSocket handshake carries no such
header and is *not* subject to the same-origin policy — any page on the
internet can open a socket to `ws://your-nas:8080/…` and the browser will
happily attach your cookies. So the handshake checks `Origin` itself, and
refuses anything that is not this instance. Getting this wrong would hand a
remote page a fully-driveable browser inside your network.

**And it carries pixels of whatever you are logged into.** That is the other
reason the checks here are not negotiable: a hijacked session is not a
defacement, it is a live view of somebody's mail.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect, status

from cairn.api.deps import AppSealer, ClientIp, Csrf, CurrentUser, DbSession
from cairn.api.errors import ApiError
from cairn.api.schemas import InteractiveSession, InteractiveStart, Ok
from cairn.db.models import AccessProfile
from cairn.db.types import utcnow
from cairn.logging import get_logger
from cairn.services import audit, browser, interactive
from cairn.services import profiles as profile_service

log = get_logger(__name__)
router = APIRouter(tags=["profiles"])

# How long to wait for a frame before sending a keepalive. Frames only arrive
# on visual change, so silence is the normal state of a settled page and the
# client must not read it as a dead connection.
FRAME_WAIT_S = 5.0
MAX_MESSAGE_BYTES = 64 * 1024


def _registry(request: Request | WebSocket) -> interactive.SessionRegistry:
    registry: interactive.SessionRegistry = request.app.state.interactive
    return registry


def _require_profile(db: DbSession, profile_id: int) -> AccessProfile:
    profile = db.get(AccessProfile, profile_id)
    if profile is None:
        raise ApiError("not_found", "That access profile does not exist.", status_code=404)
    return profile


@router.post(
    "/profiles/{profile_id}/interactive",
    response_model=InteractiveSession,
    dependencies=[Csrf],
)
async def start_session(
    profile_id: int,
    body: InteractiveStart,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    ip: ClientIp,
) -> InteractiveSession:
    profile = _require_profile(db, profile_id)
    url = body.url or profile.verify_url or "about:blank"

    try:
        session = await _registry(request).start(
            profile_id=profile.id, url=url, user_agent=profile.user_agent
        )
    except browser.BrowserUnavailableError as exc:
        raise ApiError("browser_unavailable", str(exc), status_code=503) from exc
    except interactive.InteractiveError as exc:
        raise ApiError("session_exists", str(exc), status_code=409) from exc

    audit.record(db, "profile.interactive.start", actor=user.username, target=profile.name, ip=ip)
    return InteractiveSession(
        session_id=session.id,
        profile_id=profile.id,
        url=session.page.url,
        width=browser.DEFAULT_VIEWPORT["width"],
        height=browser.DEFAULT_VIEWPORT["height"],
    )


@router.post(
    "/profiles/{profile_id}/interactive/save",
    dependencies=[Csrf],
)
async def save_session(
    profile_id: int,
    request: Request,
    db: DbSession,
    sealer: AppSealer,
    user: CurrentUser,
    ip: ClientIp,
) -> dict[str, Any]:
    """Turn whatever the browser is holding into the profile's material."""
    profile = _require_profile(db, profile_id)
    session = _registry(request).current
    if session is None or session.profile_id != profile.id:
        raise ApiError("no_session", "There is no open browser session to save.", status_code=409)

    cookies, state = await interactive.capture_state(session)
    if not cookies:
        raise ApiError(
            "nothing_to_save",
            "The browser has no cookies yet. Sign in or click through the warning first, "
            "then save.",
            status_code=409,
        )

    jar = browser.to_netscape(cookies)
    try:
        report = profile_service.store_cookies(db, sealer, profile, jar, mode="interactive")
    except profile_service.ProfileError as exc:
        raise ApiError("invalid_cookies", str(exc), status_code=422) from exc
    profile_service.store_storage_state(db, sealer, profile, state)
    profile.last_verified_at = utcnow()
    profile.last_verify_result = "ok"
    db.flush()

    audit.record(
        db,
        audit.PROFILE_MATERIAL_WRITE,
        actor=user.username,
        target=profile.name,
        ip=ip,
        detail={"kind": "interactive", "count": len(report.cookies)},
    )
    return {
        "cookie_count": len(report.cookies),
        "hosts_covered": report.hosts_covered,
        "warnings": report.warnings,
        "profile": profile_service.summary(profile),
    }


@router.delete("/profiles/{profile_id}/interactive", response_model=Ok, dependencies=[Csrf])
async def stop_session(profile_id: int, request: Request, db: DbSession, _user: CurrentUser) -> Ok:
    _require_profile(db, profile_id)
    await _registry(request).close()
    return Ok()


# ── the socket ───────────────────────────────────────────────────────────


@router.websocket("/profiles/{profile_id}/interactive/ws")
async def stream(websocket: WebSocket, profile_id: int, session_id: str = "") -> None:
    if not _origin_is_ours(websocket):
        # 1008 rather than an accept-then-close: never complete a handshake
        # with a page that had no business opening one.
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    if not _authenticated(websocket):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    registry = _registry(websocket)
    try:
        session = registry.get(session_id)
    except interactive.InteractiveError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    await websocket.send_text(json.dumps({"type": "where", "url": session.page.url}))
    # Frames only arrive on visual change, so a client attaching to a settled
    # page would otherwise see nothing and have no way to tell that from a
    # broken stream.
    await interactive.nudge(session)

    pump = asyncio.create_task(_send_frames(websocket, session))
    try:
        while True:
            raw = await websocket.receive_text()
            if len(raw) > MAX_MESSAGE_BYTES:
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            reply = await interactive.dispatch(session, message)
            if reply is not None:
                await websocket.send_text(json.dumps(reply))
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # pragma: no cover — transport-level failures
        log.warning("interactive socket failed", extra={"err": str(exc)[:200]})
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
    # The session deliberately outlives the socket: a dropped connection
    # should be reconnectable, and the reaper closes it if nobody comes back.


async def _send_frames(websocket: WebSocket, session: interactive.Session) -> None:
    while True:
        try:
            frame = await asyncio.wait_for(session.frames.get(), timeout=FRAME_WAIT_S)
        except TimeoutError:
            # Silence is normal — say so, rather than letting the client guess.
            await websocket.send_text(json.dumps({"type": "idle"}))
            continue
        await websocket.send_bytes(frame)


def _origin_is_ours(websocket: WebSocket) -> bool:
    """Refuse a handshake from any page that is not this instance.

    The same-origin policy does not apply to WebSockets: any site can open one
    to this address and the browser attaches the session cookie. `Origin` is
    the only signal in the handshake that says who asked, so it is checked
    against the Host the request arrived on, plus the configured public URL.
    """
    origin = websocket.headers.get("origin")
    if not origin:
        # A browser always sends it; something that is not a browser is not
        # carrying the user's cookies by accident either way.
        return True

    settings = websocket.app.state.settings
    allowed = {settings.app_public_url.rstrip("/")} if settings.app_public_url else set()
    host = websocket.headers.get("host")
    if host:
        allowed |= {f"http://{host}", f"https://{host}"}
    return origin.rstrip("/") in allowed or _same_host(origin, host)


def _same_host(origin: str, host: str | None) -> bool:
    if not host:
        return False
    return urlsplit(origin).netloc.lower() == host.lower()


def _authenticated(websocket: WebSocket) -> bool:
    """Resolve the session cookie by hand — WebSockets get no dependencies."""
    from cairn.services import auth

    settings = websocket.app.state.settings
    token = websocket.cookies.get(settings.cookie_name, "")
    if not token:
        return False
    factory = websocket.app.state.sessionmaker
    with factory() as session:
        return auth.resolve_session(session, token, settings) is not None
