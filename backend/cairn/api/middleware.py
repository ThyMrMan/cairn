"""Security headers.

The CSP is the app origin's half of the replay isolation story: the only
third-party frame allowed is the replay origin, and it is named explicitly
rather than wildcarded (docs/11).
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from cairn.config import Settings
from cairn.logging import get_logger

log = get_logger(__name__)

_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL | re.I)


def inline_script_hashes(index_html: Path) -> list[str]:
    """CSP hashes for the inline scripts in the built index.html.

    The page carries one inline script: it applies the stored theme before
    first paint so a dark-mode user never sees a white flash. Under
    `script-src 'self'` the browser refuses to run it, which turns the flash
    guard into dead code and puts a violation in every console — quietly,
    since nothing else breaks.

    Hashing what is actually on disk rather than pinning a constant means the
    policy cannot drift out of step with the file the moment someone edits the
    script.
    """
    try:
        html = index_html.read_text(encoding="utf-8")
    except OSError:
        return []
    hashes = []
    for match in _INLINE_SCRIPT.finditer(html):
        digest = hashlib.sha256(match.group(1).encode("utf-8")).digest()
        hashes.append(f"'sha256-{base64.b64encode(digest).decode('ascii')}'")
    return hashes


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, settings: Settings, static_dir: Path | None = None) -> None:
        super().__init__(app)
        self.settings = settings
        script_hashes = inline_script_hashes(
            (static_dir or Path(__file__).resolve().parent.parent / "static") / "index.html"
        )
        self._csp = self._build_csp(settings, script_hashes)

    @staticmethod
    def _build_csp(settings: Settings, script_hashes: list[str] | None = None) -> str:
        frame_src = settings.replay_origin or "'none'"
        script_src = " ".join(["'self'", *(script_hashes or [])])
        directives = [
            "default-src 'self'",
            f"script-src {script_src}",
            # Tailwind and the theme toggle inject inline styles; scripts stay
            # strict, which is where the actual risk is.
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: blob:",
            "font-src 'self' data:",
            f"frame-src {frame_src}",
            "connect-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "object-src 'none'",
        ]
        if settings.dev_mode:
            # Vite's dev server needs websockets for HMR and eval for the
            # module runner. Never in production.
            directives = [
                d.replace("connect-src 'self'", "connect-src 'self' ws: wss:").replace(
                    f"script-src {script_src}", f"script-src {script_src} 'unsafe-eval'"
                )
                for d in directives
            ]
        return "; ".join(directives)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("Content-Security-Policy", self._csp)
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "same-origin")
        headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=()"
        )
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if self.settings.use_secure_cookies:
            headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        # API responses are per-user and must never be cached by a proxy.
        if request.url.path.startswith("/api"):
            headers.setdefault("Cache-Control", "no-store")
        return response
