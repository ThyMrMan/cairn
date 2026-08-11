"""Running a userscript once, in a real browser, to produce a cookie jar.

The whole point of [D4](../../../docs/00-decisions.md): wget cannot execute
JavaScript, so a Tampermonkey script cannot run during a crawl. It runs here
instead, before the crawl, and what reaches the engine is the jar it produced.
The engine only ever sees `--load-cookies`, which is what makes the per-site
mode selector a choice about how the credential is obtained rather than which
crawler you are allowed to use.

**In-process rather than an engine subprocess.** docs/06 draws the mint as an
engine, and the isolation argument for that is real — this is a stranger's
JavaScript. But the isolation is already there and it is stronger than a
subprocess of ours would be: the script runs inside Chromium, in its sandbox,
in a process Playwright owns. Wrapping that in an engine manifest would add a
protocol and a spec file to something that is not a capture engine and
produces no WARC.

**Everything it learns is reported, including on failure.** A mint that fails
saves its screenshot and console output, because "here is what the browser
actually saw" is the difference between a two-minute fix and an afternoon.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cairn.logging import get_logger
from cairn.services import browser, interstitial, userscripts

log = get_logger(__name__)

# Enough for a slow interstitial that redirects twice; short enough that a
# script stuck in a loop does not hold a browser open for the afternoon.
NAVIGATION_TIMEOUT_MS = 30_000
SETTLE_TIMEOUT_MS = 10_000
MAX_CONSOLE_LINES = 40
MAX_BODY_BYTES = 512 * 1024


@dataclass(slots=True)
class MintResult:
    ok: bool
    reason: str = ""
    final_url: str = ""
    cookies_text: str | None = None
    cookie_count: int = 0
    hosts: list[str] = field(default_factory=list)
    screenshot: bytes | None = None
    console: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "final_url": self.final_url,
            "cookie_count": self.cookie_count,
            "hosts": self.hosts,
            "console": self.console,
            "warnings": self.warnings,
            "has_screenshot": self.screenshot is not None,
        }


async def mint(
    *,
    script_text: str,
    verify_url: str,
    user_agent: str | None = None,
    success_selector: str | None = None,
    interstitial_selector: str | None = None,
    body_must_not_match: str | None = None,
) -> MintResult:
    """Run the script against `verify_url` and export whatever it earned."""
    script = userscripts.parse(script_text)
    result = MintResult(ok=False, warnings=list(script.warnings))

    # Checked before a browser is launched: if the patterns do not cover the
    # verify URL, Tampermonkey would not have run it either, and saying that
    # beats reporting an empty jar half a minute later.
    covered, why = userscripts.matches_url(script, verify_url)
    if not covered:
        result.reason = why
        return result

    async with (
        browser.launched() as chromium,
        browser.context(chromium, user_agent=user_agent) as ctx,
    ):
        page = await ctx.new_page()
        console: list[str] = []
        page.on("console", lambda msg: _record(console, msg))
        page.on("pageerror", lambda err: _record_error(console, err))

        await ctx.add_init_script(userscripts.init_script(script))

        try:
            response = await page.goto(
                verify_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS
            )
        except Exception as exc:
            result.reason = f"could not load {verify_url}: {_short(exc)}"
            result.console = console[:MAX_CONSOLE_LINES]
            return result

        # The script dismisses the interstitial by clicking something, which
        # navigates. Settling matters more than any fixed wait: the jar has
        # to be read after the redirect that sets the cookie, not before it.
        await _settle(page)

        result.final_url = page.url
        result.console = console[:MAX_CONSOLE_LINES]
        with_status = response.status if response is not None else 0

        body = (await page.content()).encode("utf-8", "replace")[:MAX_BODY_BYTES]
        verdict = interstitial.judge(
            body,
            page.url,
            success_selector_found=await _has(page, success_selector),
            interstitial_selector_found=await _has(page, interstitial_selector),
            body_must_not_match=body_must_not_match,
        )

        try:
            result.screenshot = await page.screenshot(type="png")
        except Exception:  # pragma: no cover — a page that died mid-shot
            result.screenshot = None

        cookies = await ctx.cookies()
        result.cookie_count = len(cookies)
        result.hosts = sorted({str(c.get("domain") or "") for c in cookies if c.get("domain")})

        if verdict.blocked:
            result.reason = (
                f"the script ran but the page is still the interstitial — {verdict.reason}"
            )
            return result
        if not cookies:
            # Real content and no cookies means nothing was gained: the site
            # is not gated at all, and a jar of nothing would "work" in
            # testing and explain nothing when the real site refuses.
            result.reason = (
                "the page looks like real content but no cookies were set, so there "
                "is nothing to save. Is this URL actually behind the interstitial?"
            )
            return result

        result.ok = True
        result.cookies_text = browser.to_netscape(cookies)
        result.reason = f"got real content from {page.url} with {len(cookies)} cookie(s)"
        log.info(
            "mint succeeded",
            extra={"url": page.url, "cookies": len(cookies), "status": with_status},
        )
        return result


async def _settle(page: Any) -> None:
    """Wait for the click-through to finish, without insisting that it happens.

    `networkidle` is the right signal and the wrong one to require: a page with
    a long-poll or an analytics beacon never reaches it, and a mint that timed
    out waiting is indistinguishable from one that failed. So it is best
    effort, and the state of the page decides the outcome either way.
    """
    from contextlib import suppress

    with suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
    with suppress(Exception):
        await page.wait_for_load_state("domcontentloaded", timeout=2_000)


async def _has(page: Any, selector: str | None) -> bool | None:
    """Whether a selector is present, or None when none was configured."""
    if not selector:
        return None
    try:
        return bool(await page.locator(selector).count() > 0)
    except Exception:
        # An invalid selector is the user's typo, not a reason to fail the
        # mint — fall back to the heuristic and say so in the console log.
        return None


def _record(console: list[str], message: Any) -> None:
    if len(console) < MAX_CONSOLE_LINES:
        console.append(f"{message.type}: {message.text}"[:400])


def _record_error(console: list[str], error: Any) -> None:
    if len(console) < MAX_CONSOLE_LINES:
        console.append(f"pageerror: {error}"[:400])


def _short(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0][:200]
