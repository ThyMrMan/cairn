"""A real browser in the UI, so you can click through anything.

The strongest of the three profile modes and the one that needs no script and
no export extension: a Chromium session runs in the container, you drive it
from the browser tab you already have open, and whatever state that leaves
behind becomes the profile.

**A CDP screencast, not noVNC.** docs/06 planned an embedded noVNC view, which
means Xvfb, a VNC server, websockify, and an X stack in the image. Chromium
will stream the page itself over the DevTools protocol — `Page.startScreencast`
emits JPEG frames, `Input.dispatch*` sends events back — and it does that
headless, so none of that stack is needed. Measured at 1280x800 and quality
60: about 8 KB a frame and 75 KB/s while something is actually moving.

**Frames only arrive on visual change.** A settled page emits nothing at all,
which looks exactly like a broken stream and is the single most misleading
thing about this API — the first attempt at it here saw zero frames and very
nearly concluded screencast was unusable. The client is told the session is
live separately from being sent pixels, so a still page does not read as a
hang.

**One session at a time.** A browser is hundreds of megabytes of RAM and the
thing driving it is a person; there is no scenario where a single-user tool
needs two. The cap is what stops a forgotten tab from holding one open
forever, together with the idle timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from cairn.logging import get_logger
from cairn.services import browser

log = get_logger(__name__)

# Long enough to work through a multi-step login, short enough that a tab
# closed without pressing Save does not hold a browser open all night.
IDLE_TIMEOUT_S = 15 * 60
MAX_SESSION_S = 60 * 60
REAP_INTERVAL_S = 30

# Dropping frames is the right failure. A slow client that falls behind should
# see the newest state, not a growing backlog of stale ones.
FRAME_QUEUE_SIZE = 2

SCREENCAST = {"format": "jpeg", "quality": 60, "maxWidth": 1280, "maxHeight": 800}

# Keys that carry no text and have to be dispatched as key events. Everything
# printable goes through Input.insertText instead, which is far more reliable
# than synthesising keydown/keypress/keyup with the right virtual key codes —
# and getting those wrong silently produces an empty password field.
SPECIAL_KEYS = {
    "Enter": (13, "Enter"),
    "Backspace": (8, "Backspace"),
    "Tab": (9, "Tab"),
    "Delete": (46, "Delete"),
    "ArrowLeft": (37, "ArrowLeft"),
    "ArrowRight": (39, "ArrowRight"),
    "ArrowUp": (38, "ArrowUp"),
    "ArrowDown": (40, "ArrowDown"),
    "Home": (36, "Home"),
    "End": (35, "End"),
    "PageUp": (33, "PageUp"),
    "PageDown": (34, "PageDown"),
    "Escape": (27, "Escape"),
}


class InteractiveError(RuntimeError):
    """A session could not be started, driven or saved."""


@dataclass(slots=True)
class Session:
    id: str
    profile_id: int
    launch: Any
    context: Any
    page: Any
    cdp: Any
    frames: asyncio.Queue[bytes]
    started: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    closing: bool = False

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    @property
    def expired(self) -> bool:
        now = time.monotonic()
        return (now - self.last_seen) > IDLE_TIMEOUT_S or (now - self.started) > MAX_SESSION_S


class SessionRegistry:
    """Holds the one live session, and reaps it when it is forgotten."""

    def __init__(self) -> None:
        self._session: Session | None = None
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task[None] | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self, *, profile_id: int, url: str, user_agent: str | None) -> Session:
        async with self._lock:
            if self._session is not None and not self._session.expired:
                raise InteractiveError(
                    "A browser session is already open. Close it before starting another."
                )
            await self._close_locked()

            launch = await browser.start()
            try:
                context = await launch.browser.new_context(
                    # Not "HeadlessChrome": there is a person driving this, and
                    # sites that refuse a headless UA would be refusing them.
                    user_agent=user_agent or await browser.presentable_user_agent(launch.browser),
                    viewport=browser.DEFAULT_VIEWPORT,
                    accept_downloads=False,
                    service_workers="block",
                )
                page = await context.new_page()
                cdp = await context.new_cdp_session(page)
                session = Session(
                    id=secrets.token_urlsafe(18),
                    profile_id=profile_id,
                    launch=launch,
                    context=context,
                    page=page,
                    cdp=cdp,
                    frames=asyncio.Queue(maxsize=FRAME_QUEUE_SIZE),
                )
                cdp.on("Page.screencastFrame", _frame_handler(session))
                await cdp.send("Page.enable")
                await cdp.send("Page.startScreencast", SCREENCAST)
                # Not wait_until="networkidle": a login page with a long-poll
                # never reaches it, and the person can see the page load.
                with contextlib.suppress(Exception):
                    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except BaseException:
                await browser.shutdown(launch)
                raise

            self._session = session
            self._ensure_reaper()
            log.info("interactive session started", extra={"profile": profile_id, "url": url})
            return session

    def get(self, session_id: str) -> Session:
        session = self._session
        if session is None or session.id != session_id or session.closing:
            raise InteractiveError("That browser session is no longer open.")
        session.touch()
        return session

    @property
    def current(self) -> Session | None:
        return self._session

    async def close(self, session_id: str | None = None) -> None:
        async with self._lock:
            if session_id is not None and (self._session is None or self._session.id != session_id):
                return
            await self._close_locked()

    async def _close_locked(self) -> None:
        session = self._session
        self._session = None
        if session is None:
            return
        session.closing = True
        with contextlib.suppress(Exception):
            await session.cdp.send("Page.stopScreencast")
        await browser.shutdown(session.launch)
        log.info("interactive session closed", extra={"profile": session.profile_id})

    def _ensure_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap(), name="cairn-interactive-reaper")

    async def _reap(self) -> None:
        """Close a session nobody is driving any more.

        Without this, closing the browser tab leaves Chromium running for the
        life of the container — the client cannot be relied on to say goodbye,
        because the case that matters is the one where it crashed.
        """
        while True:
            await asyncio.sleep(REAP_INTERVAL_S)
            session = self._session
            if session is None:
                return
            if session.expired:
                log.info("reaping an idle interactive session", extra={"id": session.id})
                await self.close(session.id)
                return

    async def shutdown(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
        await self.close()


def _frame_handler(session: Session) -> Any:
    """Push frames without blocking Playwright's event dispatch.

    The queue is tiny and the oldest frame is dropped when it is full: a client
    that cannot keep up wants the newest picture of the page, and buffering
    would trade latency for frames nobody will ever look at.
    """
    import base64

    def handle(event: dict[str, Any]) -> None:
        if session.closing:
            return
        try:
            data = base64.b64decode(event["data"])
        except (KeyError, ValueError):  # pragma: no cover — malformed frame
            return
        with contextlib.suppress(asyncio.QueueEmpty):
            while session.frames.full():
                session.frames.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            session.frames.put_nowait(data)
        # Acknowledging is what keeps the stream flowing; without it Chromium
        # sends one frame and waits forever.
        asyncio.create_task(_ack(session, event.get("sessionId")))  # noqa: RUF006

    return handle


async def _ack(session: Session, frame_session_id: Any) -> None:
    if frame_session_id is None or session.closing:
        return
    with contextlib.suppress(Exception):
        await session.cdp.send("Page.screencastFrameAck", {"sessionId": frame_session_id})


# ── driving it ───────────────────────────────────────────────────────────


async def dispatch(session: Session, message: dict[str, Any]) -> dict[str, Any] | None:
    """Apply one client message. Returns a reply when there is something to say."""
    kind = str(message.get("type") or "")
    session.touch()

    if kind == "mouse":
        await _mouse(session, message)
    elif kind == "wheel":
        await session.cdp.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": float(message.get("x", 0)),
                "y": float(message.get("y", 0)),
                "deltaX": float(message.get("deltaX", 0)),
                "deltaY": float(message.get("deltaY", 0)),
            },
        )
    elif kind == "key":
        await _key(session, message)
    elif kind == "text":
        await session.cdp.send("Input.insertText", {"text": str(message.get("text", ""))[:2000]})
    elif kind == "navigate":
        return await _navigate(session, str(message.get("url") or ""))
    elif kind == "back":
        with contextlib.suppress(Exception):
            await session.page.go_back(timeout=15_000)
        return _where(session)
    elif kind == "forward":
        with contextlib.suppress(Exception):
            await session.page.go_forward(timeout=15_000)
        return _where(session)
    elif kind == "reload":
        with contextlib.suppress(Exception):
            await session.page.reload(timeout=30_000)
        return _where(session)
    elif kind == "where":
        return _where(session)
    return None


async def _mouse(session: Session, message: dict[str, Any]) -> None:
    action = str(message.get("action") or "move")
    kinds = {"down": "mousePressed", "up": "mouseReleased", "move": "mouseMoved"}
    await session.cdp.send(
        "Input.dispatchMouseEvent",
        {
            "type": kinds.get(action, "mouseMoved"),
            "x": float(message.get("x", 0)),
            "y": float(message.get("y", 0)),
            "button": str(message.get("button") or "left"),
            "clickCount": int(message.get("clickCount", 1)) if action != "move" else 0,
            "modifiers": int(message.get("modifiers", 0)),
        },
    )


async def _key(session: Session, message: dict[str, Any]) -> None:
    """Only keys that carry no text. Printable input goes through insertText."""
    name = str(message.get("key") or "")
    special = SPECIAL_KEYS.get(name)
    if special is None:
        return
    code, key = special
    modifiers = int(message.get("modifiers", 0))
    for kind in ("keyDown", "keyUp"):
        await session.cdp.send(
            "Input.dispatchKeyEvent",
            {
                "type": kind,
                "key": key,
                "code": key,
                "windowsVirtualKeyCode": code,
                "nativeVirtualKeyCode": code,
                "modifiers": modifiers,
            },
        )


async def _navigate(session: Session, url: str) -> dict[str, Any]:
    candidate = url.strip()
    if not candidate:
        return _where(session)
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    if not candidate.startswith(("http://", "https://")):
        return {"type": "error", "message": "Only http and https addresses can be opened."}
    with contextlib.suppress(Exception):
        await session.page.goto(candidate, wait_until="domcontentloaded", timeout=30_000)
    return _where(session)


def _where(session: Session) -> dict[str, Any]:
    return {"type": "where", "url": session.page.url}


async def nudge(session: Session) -> None:
    """Force one frame, for a page that is not painting.

    Frames only arrive on visual change, so a client that connects to a
    settled page sees nothing and cannot tell that from a broken stream.
    Restarting the screencast makes Chromium emit the current state.
    """
    with contextlib.suppress(Exception):
        await session.cdp.send("Page.stopScreencast")
        await session.cdp.send("Page.startScreencast", SCREENCAST)


# ── what it produced ─────────────────────────────────────────────────────


async def capture_state(session: Session) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The cookies and the full storage state, as they stand right now."""
    cookies = await session.context.cookies()
    state = await session.context.storage_state()
    return list(cookies), dict(state)
