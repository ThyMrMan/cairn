"""The one place Chromium is launched.

Both things that need a browser — minting a jar from a userscript, and the
interactive profile session — come through here, so the launch policy is
decided once instead of drifting between two call sites.

Three decisions worth stating, each established by running it rather than
reading about it:

**`channel="chromium"`, not the default.** The image installs the browser with
`--no-shell`, and Playwright's default headless mode runs the *headless
shell* — a separate 262 MB build. Asking for the real browser in new headless
mode avoids carrying a second one, and interactive profiles want the full
browser anyway. A plain `launch()` fails outright in this image; the build
probe is what catches that.

**The sandbox stays on.** docs/11 requires it, and containers routinely deny
the unprivileged user namespaces it needs — so it is verified at build time
and retried without at runtime rather than assumed either way. Falling back
silently would quietly drop the containment that makes running a stranger's
userscript defensible, so the fallback says so, loudly, once.

**Playwright is optional at import time.** It is a development extra, not a
core dependency, exactly like pywb: a source checkout stays installable
without a 650 MB browser, and the features that need one say why they are
unavailable instead of raising ImportError from somewhere unhelpful.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from cairn.logging import get_logger

log = get_logger(__name__)

CHANNEL = "chromium"
DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
DEFAULT_TIMEOUT_MS = 30_000
# docs/06: a userscript is user-supplied code running in your container, so it
# gets a hard ceiling rather than a generous one.
MINT_TIMEOUT_MS = 60_000

LAUNCH_ARGS = [
    # Chromium's default shared-memory use crashes it when /dev/shm is the
    # Docker default of 64 MB. The README tells people to pass --shm-size=2g;
    # this makes the tool work when they have not.
    "--disable-dev-shm-usage",
]

_sandbox_warning_given = False


class BrowserUnavailableError(RuntimeError):
    """No usable browser. The message is written to be shown to a person."""


def availability() -> tuple[bool, str]:
    """Whether a browser can be launched, and if not, what to say about it."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, (
            "Playwright is not installed, so userscript and interactive profiles are "
            "unavailable. They ship in the Docker image; a source checkout needs "
            '`pip install -e ".[dev]"` and `playwright install chromium`.'
        )

    import os
    from pathlib import Path

    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if root and not any(Path(root).glob("chromium-*")):
        return False, (
            f"Playwright is installed but no Chromium was found in {root}. "
            "Run `playwright install chromium`."
        )
    return True, ""


def require_available() -> None:
    ok, reason = availability()
    if not ok:
        raise BrowserUnavailableError(reason)


@asynccontextmanager
async def launched() -> AsyncIterator[Any]:
    """A running browser, closed on the way out however that happens.

    For work that starts and finishes inside one call — the mint. An
    interactive session outlives its request and uses `start`/`shutdown`.
    """
    require_available()
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await _launch(playwright)
        try:
            yield browser
        finally:
            await browser.close()


async def start() -> tuple[Any, Any]:
    """A browser that outlives the call, for interactive sessions.

    Returns the Playwright handle as well, because stopping it is not
    optional: it owns a Node subprocess, and dropping the reference without
    stopping leaks that process for the life of the container.
    """
    require_available()
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    try:
        browser = await _launch(playwright)
    except BaseException:
        await playwright.stop()
        raise
    return playwright, browser


async def shutdown(playwright: Any, browser: Any) -> None:
    """Close both halves, letting neither failure hide the other."""
    from contextlib import suppress

    if browser is not None:
        with suppress(Exception):
            await browser.close()
    if playwright is not None:
        with suppress(Exception):
            await playwright.stop()


async def _launch(playwright: Any) -> Any:
    try:
        return await playwright.chromium.launch(channel=CHANNEL, args=LAUNCH_ARGS)
    except Exception as exc:
        _warn_about_the_sandbox(exc)
        try:
            return await playwright.chromium.launch(
                channel=CHANNEL, args=[*LAUNCH_ARGS, "--no-sandbox"]
            )
        except Exception as fallback:
            raise BrowserUnavailableError(
                f"Chromium would not start: {fallback}. Interactive and userscript "
                "profiles need a working browser; cookies-mode profiles do not."
            ) from fallback


def _warn_about_the_sandbox(exc: Exception) -> None:
    global _sandbox_warning_given
    if _sandbox_warning_given:
        return
    _sandbox_warning_given = True
    log.warning(
        "Chromium could not start with its sandbox enabled, so it is being run "
        "without one. Userscripts are code supplied by whoever wrote them, and the "
        "sandbox is what contains them — prefer a host that allows unprivileged "
        "user namespaces. See docs/11.",
        extra={"err": str(exc).splitlines()[0][:200]},
    )


@asynccontextmanager
async def context(
    browser: Any,
    *,
    user_agent: str | None = None,
    viewport: dict[str, int] | None = None,
    storage_state: Any = None,
) -> AsyncIterator[Any]:
    """A fresh context with the restrictions docs/06 asks for.

    A context rather than reusing one: cookies must not leak between two
    profiles minted in the same process, and a context is the isolation
    boundary Playwright actually gives you.
    """
    created = await browser.new_context(
        user_agent=user_agent or None,
        viewport=viewport or DEFAULT_VIEWPORT,
        storage_state=storage_state,
        # A userscript that triggers a download would otherwise write into the
        # container. Nothing here wants files, only cookies.
        accept_downloads=False,
        # Service workers survive navigations and can keep fetching after the
        # mint believes it has finished (docs/06).
        service_workers="block",
        ignore_https_errors=False,
    )
    created.set_default_timeout(DEFAULT_TIMEOUT_MS)
    try:
        yield created
    finally:
        await created.close()


def to_netscape(cookies: list[dict[str, Any]]) -> str:
    """Playwright cookies → the Netscape jar wget loads.

    `expires` is the one that bites: Playwright reports a session cookie as
    `-1`, and Netscape spells the same thing `0`. Pass -1 through and wget
    reads a cookie that expired in 1969 and silently drops it — which for an
    interstitial bypass is usually the only cookie that mattered.
    """
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in cookies:
        domain = str(cookie.get("domain") or "")
        if not domain:
            continue
        expires = cookie.get("expires", -1)
        try:
            stamp = int(float(expires))
        except (TypeError, ValueError):
            stamp = 0
        lines.append(
            "\t".join(
                (
                    domain,
                    "TRUE" if domain.startswith(".") else "FALSE",
                    str(cookie.get("path") or "/"),
                    "TRUE" if cookie.get("secure") else "FALSE",
                    str(max(stamp, 0)),
                    str(cookie.get("name") or ""),
                    str(cookie.get("value") or ""),
                )
            )
        )
    return "\n".join(lines) + "\n"
