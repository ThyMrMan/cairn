"""Shared fixtures.

Each test gets a fresh temp directory, a fresh database and a fresh app, so
nothing leaks between tests — including the rate-limit ledger, which is
persistent by design and would otherwise lock out later tests.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.crypto.sealing import Sealer

TEST_KEY = "test-master-key-must-be-at-least-32-bytes-long"
USERNAME = "admin"
PASSWORD = "correct-horse-battery-staple"
XHR = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop a developer's .env from leaking into tests."""
    for key in list(os.environ):
        if key.startswith("CAIRN_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        secret_key=TEST_KEY,
        log_json=False,
        log_level="WARNING",
        _env_file=None,  # type: ignore[call-arg]
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    from cairn.app import create_app

    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db(client: TestClient) -> Iterator[Session]:
    """A session against the same database the client uses."""
    factory = client.app.state.sessionmaker  # type: ignore[attr-defined]
    with factory() as session:
        yield session


@pytest.fixture
def sealer() -> Sealer:
    return Sealer(TEST_KEY.encode())


@pytest.fixture
def authed(client: TestClient) -> TestClient:
    """A client that has completed setup and is logged in."""
    res = client.post("/api/setup", json={"username": USERNAME, "password": PASSWORD}, headers=XHR)
    assert res.status_code == 201, res.text
    return client


# ── a small site to capture, shared by the end-to-end tests ──────────────
#
# Lives here rather than in one of them because both the capture and the
# replay suites need the same fixture site, and a fixture imported from
# another test module is a fixture pytest and ruff disagree about.

PAGES: dict[str, tuple[str, bytes]] = {
    "/": (
        "text/html",
        b"""<html><body><h1>Index</h1>
        <a href="/post-1.html">one</a>
        <a href="/post-2.html">two</a>
        <a href="/missing.html">gone</a>
        <img src="/logo.png">
        </body></html>""",
    ),
    "/post-1.html": (
        "text/html",
        b"<html><head><style>"
        # Exactly how a Blogger skin writes its theme image. wget does not
        # decode CSS escapes, so it requests this against the blog and 404s.
        rb"body{background:url(https\:\/\/themes.example.test\/image?id=abc)}"
        b"</style></head><body><h1>Post One</h1><p>UNIQUE-CONTENT-MARKER-ONE</p>"
        b'<img data-src="/lazy.png"></body></html>',
    ),
    "/post-2.html": (
        "text/html",
        b"<html><body><h1>Post Two</h1><p>UNIQUE-CONTENT-MARKER-TWO</p></body></html>",
    ),
    "/logo.png": ("image/png", b"\x89PNG\r\n\x1a\n" + b"LOGO" * 32),
}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/robots.txt":
            body = b"User-agent: *\nAllow: /\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        hit = PAGES.get(path)
        if hit is None:
            body = b"<html><body>not found</body></html>"
            self.send_response(404)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        ctype, body = hit
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def site_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


# ── a site behind a content warning, for the M5 mint ─────────────────────
#
# The same shape as Blogger's: everything is the interstitial until a cookie
# is present, and the interstitial sets that cookie from a button. No real
# site and no real account is involved in testing the bypass.

GATE_COOKIE = "cairn_test_consent"

# The button is positioned absolutely, and accepting lands on a *different*
# path. Both are for the interactive test, which drives this through a
# screencast: it can only send a coordinate, so where the button is must not
# depend on default font metrics, and "did the click work?" has to be
# answerable from the URL rather than from pixels.
INTERSTITIAL_PAGE = (
    b"<html><body style='margin:0'><h1>Content warning</h1>"
    b"<p>This blog may contain content only suitable for adults.</p>"
    b'<button id="continue" onclick="accept()" '
    b"style='position:absolute;left:0;top:200px;width:600px;height:120px'>"
    b"I understand and wish to continue</button>"
    b"<script>function accept(){document.cookie='" + GATE_COOKIE.encode() + b"=1; path=/';"
    b"location.href='/post-1.html';}</script></body></html>"
)
# Anywhere inside the button above.
INTERSTITIAL_BUTTON = (300, 260)

GATED_PAGES: dict[str, bytes] = {
    "/": b"<html><body><h1>Index</h1><a href='/post-1.html'>one</a>"
    b"<p>UNIQUE-GATED-INDEX</p></body></html>",
    "/post-1.html": b"<html><body><h1>Post One</h1><p>UNIQUE-GATED-CONTENT</p></body></html>",
}


class _GatedHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/robots.txt":
            self._send(b"User-agent: *\nAllow: /\n", "text/plain")
            return
        if GATE_COOKIE not in (self.headers.get("Cookie") or ""):
            self._send(INTERSTITIAL_PAGE, "text/html")
            return
        body = GATED_PAGES.get(path)
        if body is None:
            self._send(b"<html><body>not found</body></html>", "text/html", status=404)
            return
        self._send(body, "text/html")

    def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def gated_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GatedHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


# ── a blog that can publish, for the M6 feed watch ───────────────────────
#
# The one thing the fixtures above cannot do: change while the test is
# running. Watching for new content is untestable against a static site, and
# the whole milestone is about what happens when something appears.


class Blog:
    """A small blog with an Atom feed, which can gain a post mid-test."""

    def __init__(self) -> None:
        self.base = ""
        self.posts: list[tuple[str, str]] = []  # (slug, marker)
        self.feed_requests = 0
        self.conditional_requests = 0
        # Bumped on publish, so a conditional GET can be answered honestly.
        self.etag = "v1"
        self.publish("post-1", "MARKER-ONE")
        self.publish("post-2", "MARKER-TWO")

    def publish(self, slug: str, marker: str) -> None:
        self.posts.append((slug, marker))
        self.etag = f"v{len(self.posts)}"

    # ── what it serves ───────────────────────────────────────────────────

    def index(self) -> bytes:
        links = "".join(f"<a href='/{slug}.html'>{slug}</a>" for slug, _ in self.posts)
        return f"<html><body><h1>Index</h1>{links}</body></html>".encode()

    def post(self, slug: str) -> bytes | None:
        for known, marker in self.posts:
            if known == slug:
                # Links only to the index, so a depth-1 capture provably stops
                # there instead of reaching the other posts through it.
                return (
                    f"<html><body><h1>{known}</h1><p>{marker}</p>"
                    f"<a href='/'>home</a><img src='/logo.png'></body></html>"
                ).encode()
        return None

    def feed(self) -> bytes:
        entries = "".join(
            f"<entry><id>urn:post:{slug}</id><title>{slug}</title>"
            f"<link rel='alternate' type='text/html' href='{self.base}{slug}.html'/>"
            f"<published>2026-08-0{i + 1}T00:00:00Z</published></entry>"
            for i, (slug, _) in enumerate(self.posts)
        )
        return (
            "<?xml version='1.0' encoding='utf-8'?>"
            "<feed xmlns='http://www.w3.org/2005/Atom'><title>Test Blog</title>"
            f"{entries}</feed>"
        ).encode()

    def sitemap(self) -> bytes:
        urls = "".join(f"<url><loc>{self.base}{slug}.html</loc></url>" for slug, _ in self.posts)
        return (
            "<?xml version='1.0' encoding='utf-8'?>"
            "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
            f"{urls}</urlset>"
        ).encode()


class _BlogHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    blog: Blog

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/robots.txt":
            self._send(b"User-agent: *\nAllow: /\n", "text/plain")
        elif path in ("/feed.xml", "/sitemap.xml"):
            self.blog.feed_requests += 1
            if self.headers.get("If-None-Match") == self.blog.etag:
                self.blog.conditional_requests += 1
                self.send_response(304)
                self.send_header("ETag", self.blog.etag)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = self.blog.feed() if path == "/feed.xml" else self.blog.sitemap()
            self._send(body, "application/xml", etag=self.blog.etag)
        elif path == "/":
            self._send(self.blog.index(), "text/html")
        elif path == "/logo.png":
            self._send(b"\x89PNG\r\n\x1a\n" + b"LOGO" * 32, "image/png")
        elif path.endswith(".html") and (body := self.blog.post(path[1:-5])) is not None:
            self._send(body, "text/html")
        else:
            self._send(b"<html><body>not found</body></html>", "text/html", status=404)

    def _send(self, body: bytes, ctype: str, status: int = 200, etag: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def blog() -> Iterator[Blog]:
    """A running blog. `blog.publish(...)` makes new content appear."""
    state = Blog()
    handler = type("_Bound", (_BlogHandler,), {"blog": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state.base = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


class _WebhookHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    received: list[dict[str, object]]

    def do_POST(self) -> None:
        import json

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {"raw": raw.decode("utf-8", errors="replace")}
        self.received.append({"path": self.path, "payload": payload})
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def webhook() -> Iterator[tuple[str, list[dict[str, object]]]]:
    """A URL to notify, and the list it appends to. (url, received)"""
    received: list[dict[str, object]] = []
    handler = type("_Bound", (_WebhookHandler,), {"received": received})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/hook", received
    finally:
        server.shutdown()
        server.server_close()
