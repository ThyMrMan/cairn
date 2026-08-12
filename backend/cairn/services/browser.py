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

import os
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
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
    # Stops Blink setting `navigator.webdriver = true`. Together with dropping
    # `--enable-automation` below, this removes the two signals a driven
    # browser announces about itself for no benefit to anyone here — the
    # person at the keyboard is a person, and a site refusing them because
    # the window is remote is refusing the wrong thing.
    "--disable-blink-features=AutomationControlled",
]

# Playwright passes this to mark the browser as test-driven. It is what sets
# `navigator.webdriver`, and it also puts an infobar on a headed window.
SUPPRESSED_DEFAULT_ARGS = ["--enable-automation"]

_sandbox_warning_given = False


class BrowserUnavailableError(RuntimeError):
    """No usable browser. The message is written to be shown to a person."""


def _browser_roots() -> list[Path]:
    """Where Playwright keeps its browsers, in the order it looks.

    `PLAYWRIGHT_BROWSERS_PATH` when set — the image sets it, because the
    default is under the *building* user's home while the container runs as
    `abc` with `HOME=/config` (see the Dockerfile). Otherwise Playwright's own
    per-platform default, which is what a source checkout gets.
    """
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if root:
        return [Path(root)]
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return [Path(base) / "ms-playwright"]
    if sys.platform == "darwin":
        return [Path.home() / "Library" / "Caches" / "ms-playwright"]
    return [Path.home() / ".cache" / "ms-playwright"]


def availability() -> tuple[bool, str]:
    """Whether a browser can be launched, and if not, what to say about it.

    The browser is checked for, not assumed. This used to look only inside
    `PLAYWRIGHT_BROWSERS_PATH` and report "available" whenever that variable
    was unset — which is every source checkout, and every CI runner, where
    `pip install -e ".[dev]"` installs the *package* and nothing downloads the
    *browser*. The result was the opposite of what this function is for: a
    confident yes, followed by Playwright raising "Executable doesn't exist"
    from somewhere with no advice attached.
    """
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, (
            "Playwright is not installed, so userscript and interactive profiles are "
            "unavailable. They ship in the Docker image; a source checkout needs "
            '`pip install -e ".[dev]"` and `playwright install chromium`.'
        )

    roots = _browser_roots()
    if not any(root.is_dir() and any(root.glob("chromium*")) for root in roots):
        where = ", ".join(str(root) for root in roots)
        return False, (
            f"Playwright is installed but no Chromium was found in {where}. "
            "Run `playwright install chromium`."
        )
    return True, ""


def require_available() -> None:
    ok, reason = availability()
    if not ok:
        raise BrowserUnavailableError(reason)


@dataclass(slots=True)
class Launch:
    """A running browser and everything that has to be cleaned up with it."""

    playwright: Any
    browser: Any
    home: Path


@asynccontextmanager
async def launched() -> AsyncIterator[Any]:
    """A running browser, closed on the way out however that happens.

    For work that starts and finishes inside one call — the mint. An
    interactive session outlives its request and uses `start`/`shutdown`.
    """
    launch = await start()
    try:
        yield launch.browser
    finally:
        await shutdown(launch)


async def start() -> Launch:
    """A browser that outlives the call, for interactive sessions.

    Returns the Playwright handle too, because stopping it is not optional: it
    owns a Node subprocess, and dropping the reference without stopping leaks
    that process for the life of the container.
    """
    require_available()
    from playwright.async_api import async_playwright

    home = Path(tempfile.mkdtemp(prefix="cairn-browser-"))
    playwright = await async_playwright().start()
    try:
        browser = await _launch(playwright, home)
    except BaseException:
        await playwright.stop()
        shutil.rmtree(home, ignore_errors=True)
        raise
    return Launch(playwright=playwright, browser=browser, home=home)


async def shutdown(launch: Launch | None) -> None:
    """Close every part, letting no one failure hide another."""
    from contextlib import suppress

    if launch is None:
        return
    if launch.browser is not None:
        with suppress(Exception):
            await launch.browser.close()
    if launch.playwright is not None:
        with suppress(Exception):
            await launch.playwright.stop()
    shutil.rmtree(launch.home, ignore_errors=True)


def _environment(home: Path) -> dict[str, str]:
    """The browser's environment, with a home it can definitely write to.

    Chromium derives its crash-dump database path from the user's home. Given
    one it cannot write, it hands `chrome_crashpad_handler` an empty
    `--database`, crashpad refuses, and the browser dies with SIGTRAP before
    ever speaking the DevTools protocol — reported as "Target page, context or
    browser has been closed", which points nowhere near the cause.

    That is not hypothetical: the app runs under `s6-setuidgid abc`, which
    changes uid and gid and deliberately leaves HOME alone, so a process
    running as uid 99 inherits `HOME=/root`. Every other way of starting the
    browser — the build probe, `docker exec`, a development shell — supplies a
    writable home and works, which is precisely why this reached a release.

    Curiously, HOME *unset* is fine; Chromium falls back sensibly. It is HOME
    pointing somewhere unwritable that breaks it. So rather than depend on how
    the process was started, every launch gets its own directory.
    """
    return {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
    }


async def _launch(playwright: Any, home: Path) -> Any:
    env = _environment(home)
    try:
        return await playwright.chromium.launch(
            channel=CHANNEL,
            args=LAUNCH_ARGS,
            env=env,
            ignore_default_args=SUPPRESSED_DEFAULT_ARGS,
        )
    except Exception as exc:
        # Only retry without the sandbox when the sandbox is what failed.
        # Blanket-retrying drops a security control for unrelated reasons —
        # and it did: an unwritable HOME was answered by disabling the sandbox
        # and telling the user to go and fix their user namespaces.
        if not _looks_like_a_sandbox_failure(exc):
            raise BrowserUnavailableError(
                f"Chromium would not start: {_first_line(exc)}. Interactive and userscript "
                "profiles need a working browser; cookies-mode profiles do not."
            ) from exc
        _warn_about_the_sandbox(exc)
        try:
            return await playwright.chromium.launch(
                channel=CHANNEL,
                args=[*LAUNCH_ARGS, "--no-sandbox"],
                env=env,
                ignore_default_args=SUPPRESSED_DEFAULT_ARGS,
            )
        except Exception as fallback:
            raise BrowserUnavailableError(
                f"Chromium would not start even without its sandbox: {_first_line(fallback)}."
            ) from fallback


# What Chromium says when its sandbox is the problem. Anything else is a
# different problem wearing the same exception type.
_SANDBOX_MARKERS = (
    "no usable sandbox",
    "sandbox helper",
    "suid sandbox",
    "user namespace",
    "clone_newuser",
    "operation not permitted",
)


def _looks_like_a_sandbox_failure(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _SANDBOX_MARKERS)


def _first_line(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0][:300]


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
        extra={"err": _first_line(exc)},
    )


async def presentable_user_agent(browser: Any) -> str | None:
    """The browser's own user agent, minus the word `Headless`.

    Headless Chromium announces itself as `HeadlessChrome/151.0.0.0`, and
    plenty of sites treat that as a bot regardless of who is driving. There is
    a person at the keyboard here, so saying `Chrome` is the accurate
    description of what is rendering the page — it is the same browser, the
    same engine and the same version, just not advertising that its window is
    somewhere else.

    Read from the running browser rather than written down, because a
    hard-coded string is wrong the day Chromium updates and nothing notices.
    """
    try:
        session = await browser.new_browser_cdp_session()
        version = await session.send("Browser.getVersion")
        await session.detach()
    except Exception:  # pragma: no cover — CDP unavailable
        return None
    agent = str(version.get("userAgent") or "")
    if not agent:
        return None
    return agent.replace("HeadlessChrome/", "Chrome/")


@asynccontextmanager
async def context(
    browser: Any,
    *,
    user_agent: str | None = None,
    viewport: dict[str, int] | None = None,
    storage_state: Any = None,
    device_scale_factor: float | None = None,
) -> AsyncIterator[Any]:
    """A fresh context with the restrictions docs/06 asks for.

    A context rather than reusing one: cookies must not leak between two
    profiles minted in the same process, and a context is the isolation
    boundary Playwright actually gives you.

    The profile's own user agent wins when it has one — docs/06 is explicit
    that some bypasses bind the cookie to it, so a mint that quietly used a
    different one would produce a jar that fails in the crawl.

    `device_scale_factor` below 1 renders at less than CSS resolution, which is
    how the site thumbnail gets a card-sized image out of a full-sized viewport
    without an imaging library anywhere in the dependency tree.
    """
    created = await browser.new_context(
        user_agent=user_agent or await presentable_user_agent(browser),
        viewport=viewport or DEFAULT_VIEWPORT,
        device_scale_factor=device_scale_factor,
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


def storage_state_from_jar(path: str | None) -> dict[str, Any] | None:
    """A Netscape `cookies.txt` → the storage state a context is built from.

    The inverse of `to_netscape`, and needed for the same reason discovery's
    HTTP client loads the jar: a browser sent at a flagged blog without the
    cookie renders the content warning, and everything downstream then
    describes the warning rather than the blog.

    `expires` inverts the same trap. Netscape spells a session cookie `0`;
    Playwright spells it `-1`, and passing 0 through sets a cookie that expired
    at the epoch — which Chromium drops on the way in, silently, leaving a
    context that looks like it has the jar and does not.
    """
    if not path:
        return None
    from cairn.services import profiles

    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("could not read cookies for the browser", extra={"err": str(exc)})
        return None

    report = profiles.parse_cookies(text)
    cookies: list[dict[str, Any]] = [
        {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path or "/",
            "expires": -1 if cookie.is_session else float(cookie.expires),
            "httpOnly": cookie.http_only,
            "secure": cookie.secure,
            "sameSite": "Lax",
        }
        for cookie in report.cookies
    ]
    if not cookies:
        return None
    return {"cookies": cookies, "origins": []}


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
