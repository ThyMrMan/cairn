"""Discovery through a real browser.

The plain discovery path fetches HTML and reads its attributes. That is fast,
dependency-free and correct about everything a server sends — and structurally
blind to anything a page decides on at runtime. Three gaps, all measured
against a fixture built to expose them:

  - **A host referenced only from JavaScript is invisible.** `htmlrefs` does
    not read script *bodies*, deliberately: guessing at strings inside
    JavaScript invents requests. So `new Image().src = "//cdn/pixel.gif"` is
    simply not there.
  - **A link the script appends is invisible**, so the sample never reaches
    the pages behind it.
  - **Content behind infinite scroll is invisible**, because nothing scrolls.

The correction that matters is the third one, and it is not the obvious
implementation. Rendering the page and re-parsing the resulting DOM catches
the injected link and any element the script *inserted* — but `new Image()`
never enters the DOM, so the DOM re-parse misses the very host this feature
exists to find. Measured: with the fixture above, the rendered DOM yielded two
asset hosts and the network log yielded three, and the missing one was the
JavaScript-only pixel.

So the browser's own **network log** is the evidence, and the rendered DOM is
used only for links. The log also carries each response's real content type,
which is better data than the plain path has: it only learns a MIME type for
pages it fetched itself, and guesses at the rest from the URL.

This is opt-in per run. It needs the Chromium that ships for M5, it is roughly
an order of magnitude slower than fetching, and for a site that serves its
content as HTML it finds exactly the same thing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from cairn.logging import get_logger

log = get_logger(__name__)

# A page that never goes quiet — a long poll, an open socket, an ad that
# refreshes — would otherwise hold the whole run. Reaching this is normal and
# not an error: whatever loaded by then is still a better sample than markup.
IDLE_TIMEOUT_MS = 8_000
NAV_TIMEOUT_MS = 30_000

# How many times to jump to the bottom and wait. Three is enough to trigger the
# common "load more when you reach the end" pattern twice over; a feed that
# paginates forever is not something discovery should exhaust anyway.
DEFAULT_SCROLL_PASSES = 3
SCROLL_SETTLE_MS = 400

# Rendering is slow, and discovery is meant to be cheap enough to re-run. A
# hundred pages through a browser is minutes of wall clock where the plain path
# takes seconds, so browser runs are clamped and told they were.
BROWSER_PAGE_CEILING = 40


def clamp_pages(max_pages: int) -> tuple[int, str | None]:
    """How many pages a browser run may sample, and what to say if it is fewer."""
    if max_pages <= BROWSER_PAGE_CEILING:
        return max_pages, None
    return BROWSER_PAGE_CEILING, (
        f"Rendering is capped at {BROWSER_PAGE_CEILING} pages rather than the {max_pages} "
        "asked for. A browser takes seconds per page where a fetch takes milliseconds, and "
        "the sample only has to be big enough to see every host the template uses."
    )


@dataclass(slots=True)
class SubRequest:
    """One thing the page asked for, as the browser recorded it."""

    url: str
    resource_type: str
    mime: str = ""


@dataclass(slots=True)
class Rendered:
    """One page after its scripts have had their say."""

    url: str
    status: int
    html: str = ""
    # The bytes the server actually sent, when they could be read back. Kept so
    # the run can say what the browser found that markup alone would not have —
    # which is the only honest answer to "was rendering worth it".
    served_html: str | None = None
    content_type: str = ""
    error: str | None = None
    requests: list[SubRequest] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type.lower()


class Renderer:
    """A browser held open for one discovery run.

    Mirrors `Fetcher`'s shape — an async context manager with `get` — so the
    sampling crawl can take either one without knowing which it has.
    """

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        cookies_file: str | None = None,
        storage_state: dict[str, Any] | None = None,
        scroll_passes: int = DEFAULT_SCROLL_PASSES,
        wait_s: float = 0.0,
    ) -> None:
        self.user_agent = user_agent
        self.cookies_file = cookies_file
        self.storage_state = storage_state
        self.scroll_passes = max(0, scroll_passes)
        self.wait_s = wait_s
        self._launch: Any = None
        self._context: Any = None

    async def __aenter__(self) -> Renderer:
        from cairn.services import browser

        self._launch = await browser.start()
        # A profile's full browser state wins over its cookie jar when it has
        # one: a site whose login lives in localStorage renders as the sign-in
        # page from cookies alone, and discovery would then describe *that*.
        storage = self.storage_state or (
            browser.storage_state_from_jar(self.cookies_file) if self.cookies_file else None
        )
        self._context = await self._launch.browser.new_context(
            user_agent=self.user_agent
            or await browser.presentable_user_agent(self._launch.browser),
            viewport=browser.DEFAULT_VIEWPORT,
            storage_state=storage,
            # Discovery reads. A site that starts a download during a probe is
            # not something to write into the container.
            accept_downloads=False,
            service_workers="block",
            ignore_https_errors=False,
        )
        self._context.set_default_timeout(NAV_TIMEOUT_MS)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        from contextlib import suppress

        from cairn.services import browser

        if self._context is not None:
            with suppress(Exception):
                await self._context.close()
            self._context = None
        await browser.shutdown(self._launch)
        self._launch = None

    async def get(self, url: str) -> Rendered:
        assert self._context is not None, "use Renderer as an async context manager"
        if self.wait_s:
            await asyncio.sleep(self.wait_s)

        seen: list[SubRequest] = []
        mimes: dict[str, str] = {}
        result = Rendered(url=url, status=0)
        page = await self._context.new_page()
        try:
            page.on(
                "request",
                lambda request: seen.append(SubRequest(request.url, request.resource_type)),
            )
            page.on(
                "response",
                lambda response: mimes.setdefault(
                    response.url, response.headers.get("content-type", "")
                ),
            )
            response = await page.goto(url, wait_until="domcontentloaded")
            if response is None:
                result.error = "the browser navigated to nothing"
                return result
            result.status = response.status
            result.content_type = response.headers.get("content-type", "")
            result.served_html = await _served_body(response)

            await _settle(page)
            for _ in range(self.scroll_passes):
                if not await _scroll_to_bottom(page):
                    break
                await _settle(page)

            result.html = await page.content()
        except Exception as exc:
            # A page that will not render is a finding, not a crash: the run
            # carries on with the pages that do.
            result.error = f"{type(exc).__name__}: {exc}".strip().splitlines()[0][:300]
        finally:
            from contextlib import suppress

            with suppress(Exception):
                await page.close()

        for request in seen:
            request.mime = (mimes.get(request.url) or "").split(";")[0].strip().lower()
        result.requests = seen
        if not result.content_type and result.html:
            result.content_type = "text/html"
        return result


async def _served_body(response: Any) -> str | None:
    """The document as the server sent it, for the did-rendering-help report.

    Best effort by design. Chromium does not always retain a navigation body,
    and re-fetching it to be sure would double every request for a comparison
    that is a nicety.
    """
    try:
        return str(await response.text())
    except Exception:
        return None


async def _settle(page: Any) -> None:
    from contextlib import suppress

    with suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=IDLE_TIMEOUT_MS)


async def _scroll_to_bottom(page: Any) -> bool:
    """Jump to the bottom. False once the page stops getting taller.

    Height rather than a fixed number of passes: a page that is not an
    infinite feed reaches its end on the first jump, and three more scrolls of
    a static page is three more seconds for nothing.
    """
    try:
        before = int(await page.evaluate("document.body.scrollHeight"))
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(SCROLL_SETTLE_MS)
        after = int(await page.evaluate("document.body.scrollHeight"))
    except Exception:
        return False
    return after > before
