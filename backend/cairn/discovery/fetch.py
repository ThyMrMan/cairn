"""The HTTP client discovery uses.

Discovery talks to sites nobody vetted, so every fetch is bounded in three
ways that the crawl itself cannot rely on being polite about: a response size
cap, a total timeout, and a redirect cap. A discovery pass that hangs on one
slow host, or pulls a 4 GB "HTML" file into memory, is worse than one that
gives up and says so — it blocks the queue and the user learns nothing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx

from cairn.logging import get_logger

log = get_logger(__name__)

MAX_BODY_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT_S = 20.0
MAX_REDIRECTS = 5
USER_AGENT = "Mozilla/5.0 (compatible; Cairn/1.0; +https://github.com/ThyMrMan/cairn)"


@dataclass(slots=True)
class Fetched:
    url: str
    status: int
    body: bytes
    content_type: str
    error: str | None = None
    truncated: bool = False
    # Response headers, lowercased. Feed polling needs ETag and Last-Modified
    # back out so the next poll can ask conditionally (docs/08).
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300

    @property
    def not_modified(self) -> bool:
        """A conditional GET that the server answered "nothing changed".

        Distinct from `ok`, and distinct from a failure. A 304 is the good
        outcome of a poll and the reason a short interval is affordable at all.
        """
        return self.error is None and self.status == 304

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type.lower()

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


@dataclass(slots=True)
class Fetcher:
    """A bounded, polite HTTP client for one discovery run."""

    user_agent: str = USER_AGENT
    timeout_s: float = DEFAULT_TIMEOUT_S
    wait_s: float = 0.0
    cookies_file: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    _client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Fetcher:
        cookies = _load_cookie_jar(self.cookies_file) if self.cookies_file else None
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            timeout=httpx.Timeout(self.timeout_s),
            headers={"User-Agent": self.user_agent, **self.headers},
            cookies=cookies,
            # Discovery reads; it must never be talked into writing.
            trust_env=False,
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get(self, url: str, headers: dict[str, str] | None = None) -> Fetched:
        assert self._client is not None, "use Fetcher as an async context manager"
        if self.wait_s:
            await asyncio.sleep(self.wait_s)
        try:
            async with self._client.stream("GET", url, headers=headers) as response:
                chunks: list[bytes] = []
                size = 0
                truncated = False
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= MAX_BODY_BYTES:
                        truncated = True
                        break
                return Fetched(
                    url=str(response.url),
                    status=response.status_code,
                    body=b"".join(chunks)[:MAX_BODY_BYTES],
                    content_type=response.headers.get("content-type", ""),
                    truncated=truncated,
                    headers={k.lower(): v for k, v in response.headers.items()},
                )
        except (httpx.HTTPError, ssl_error_types()) as exc:
            return Fetched(url=url, status=0, body=b"", content_type="", error=str(exc)[:300])
        except Exception as exc:  # pragma: no cover — malformed URLs, odd encodings
            return Fetched(
                url=url, status=0, body=b"", content_type="", error=f"{type(exc).__name__}: {exc}"
            )

    async def head_or_get(self, url: str) -> Fetched:
        """Probe for existence.

        A plain HEAD is not enough: plenty of servers answer HEAD with 405 or
        a lie and then serve the resource perfectly well on GET, so a HEAD-only
        probe reports feeds and sitemaps as absent when they are right there.
        """
        assert self._client is not None
        try:
            response = await self._client.head(url)
            if response.status_code < 400:
                return Fetched(
                    url=str(response.url),
                    status=response.status_code,
                    body=b"",
                    content_type=response.headers.get("content-type", ""),
                )
        except httpx.HTTPError:
            pass
        return await self.get(url)


def ssl_error_types() -> type[BaseException]:
    import ssl

    return ssl.SSLError


def _load_cookie_jar(path: str) -> Any:
    """Load a Netscape cookies.txt for discovery behind an interstitial.

    Discovery has to see what a reader sees. Without the jar, a flagged blog
    answers every probe with the content warning, and the domain picker ends
    up describing the interstitial's hosts rather than the blog's.
    """
    from http.cookiejar import MozillaCookieJar

    jar = MozillaCookieJar(path)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, ValueError) as exc:
        log.warning("could not load cookies for discovery", extra={"err": str(exc)})
        return None
    return jar
