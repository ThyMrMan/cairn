"""Security headers.

The CSP is the app origin's half of the replay isolation story: the only
third-party frame allowed is the replay origin, and it is named explicitly
rather than wildcarded (docs/11).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from cairn.config import Settings


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self.settings = settings
        self._csp = self._build_csp(settings)

    @staticmethod
    def _build_csp(settings: Settings) -> str:
        frame_src = settings.replay_origin or "'none'"
        directives = [
            "default-src 'self'",
            "script-src 'self'",
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
                    "script-src 'self'", "script-src 'self' 'unsafe-eval'"
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
