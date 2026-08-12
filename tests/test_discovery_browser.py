"""Browser-based discovery: what rendering finds that fetching cannot.

The fixture is four loopback addresses rather than one, because the claim is
about *hosts* and the domain picker's rows are per host. Each address is
reachable only by a different route:

  - 127.0.0.1 serves the blog
  - 127.0.0.2 is named only inside a script, by `new Image()`, and never
    enters the DOM at all
  - 127.0.0.3 is named in plain markup on a page reachable only through a link
    the script appends
  - 127.0.0.4 is named in plain markup on a page reachable only after scrolling

So the three ways a browser can find a host — the network log, a rendered
link, and a scroll — each have exactly one host that proves them, and the
plain run is the control rather than an argument.

These need Chromium and run in the container and in CI, like the mint and
browsertrix suites. Chromium is a separate process with its own resolver, so
this cannot use the `getaddrinfo` patch the M2 fixture relies on; literal
loopback addresses need no resolution from anybody.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cairn.discovery import render
from cairn.discovery.runner import DiscoveryOptions, discover
from cairn.services import browser

needs_browser = pytest.mark.skipif(
    not browser.availability()[0], reason="needs Playwright and Chromium"
)

BLOG_IP = "127.0.0.1"
PIXEL_IP = "127.0.0.2"
LINKED_IP = "127.0.0.3"
SCROLLED_IP = "127.0.0.4"

GIF = bytes.fromhex(
    "47494638396101000100800000ffffff00000021f90401000000002c00000000010001000002024401003b"
)

INDEX = """<!doctype html>
<html><head><title>Fixture blog</title><link rel="stylesheet" href="/style.css"></head>
<body>
<h1>Fixture blog</h1>
<a href="/post1.html">Post one</a>
<img src="/plain.png" alt="plain">
<div id="more"></div>
<script>
// Assembled rather than written out, for the reason the M7 fixture records:
// a literal URL-shaped attribute inside a script is found by text-scanning
// parsers, and a fixture meant to prove "only a browser sees this" would then
// be proving nothing.
var pixel = new Image();
pixel.src = "http://{pixel}/pi" + "xel.gif";
var a = document.createElement("a");
a.setAttribute("h" + "ref", "/post2" + ".html");
a.textContent = "Post two";
document.body.appendChild(a);
var loaded = false;
window.addEventListener("scroll", function () {{
  if (loaded) return;
  if (window.scrollY + window.innerHeight < document.body.scrollHeight - 50) return;
  loaded = true;
  var b = document.createElement("a");
  b.setAttribute("h" + "ref", "/post3" + ".html");
  b.textContent = "Post three";
  document.getElementById("more").appendChild(b);
}});
</script>
<div style="height: 3000px"></div>
</body></html>"""

PLAIN_INDEX = """<!doctype html>
<html><head><title>Ordinary blog</title></head><body>
<h1>Ordinary blog</h1>
<a href="/post1.html">Post one</a>
<img src="/plain.png" alt="plain">
</body></html>"""

POST = """<!doctype html><html><head><title>{title}</title></head>
<body><h1>{title}</h1>{extra}</body></html>"""


@dataclass(slots=True)
class Fixture:
    seed: str
    plain_seed: str
    hosts: dict[str, str]  # ip -> "ip:port"


def _body_for(path: str, hosts: dict[str, str]) -> tuple[bytes, str]:
    if path in ("/", "/index.html"):
        return INDEX.format(pixel=hosts[PIXEL_IP]).encode(), "text/html"
    if path == "/plain/":
        return PLAIN_INDEX.encode(), "text/html"
    if path == "/post1.html":
        return POST.format(title="Post one", extra="<p>nothing special</p>").encode(), "text/html"
    if path == "/post2.html":
        img = f'<img src="http://{hosts[LINKED_IP]}/photo.png">'
        return POST.format(title="Post two", extra=img).encode(), "text/html"
    if path == "/post3.html":
        img = f'<img src="http://{hosts[SCROLLED_IP]}/photo.png">'
        return POST.format(title="Post three", extra=img).encode(), "text/html"
    if path.endswith(".css"):
        return b"body{font-family:sans-serif}", "text/css"
    return GIF, "image/gif"


def _serve(ip: str, hosts: dict[str, str]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            body, ctype = _body_for(self.path.split("?")[0], hosts)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer((ip, 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def js_hosts() -> Iterator[Fixture]:
    """Four loopback addresses, each proving a different way of being found."""
    addresses = [BLOG_IP, PIXEL_IP, LINKED_IP, SCROLLED_IP]
    try:
        probe = socket.socket()
        probe.bind((SCROLLED_IP, 0))
        probe.close()
    except OSError:  # pragma: no cover — a platform without 127.0.0.0/8
        pytest.skip("this platform does not offer more than one loopback address")

    hosts: dict[str, str] = {}
    servers: list[ThreadingHTTPServer] = []
    for ip in addresses:
        server = _serve(ip, hosts)
        servers.append(server)
        hosts[ip] = f"{ip}:{server.server_address[1]}"

    blog = hosts[BLOG_IP]
    try:
        yield Fixture(
            seed=f"http://{blog}/",
            plain_seed=f"http://{blog}/plain/",
            hosts=hosts,
        )
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def _options(**kwargs: object) -> DiscoveryOptions:
    # Small and quick: the fixture is five pages and the point is which hosts
    # come out, not how deep a crawl can go.
    return DiscoveryOptions(max_pages=10, max_depth=2, concurrency=2, **kwargs)  # type: ignore[arg-type]


# ── the control ──────────────────────────────────────────────────────────


async def test_fetching_finds_only_the_host_the_html_names(js_hosts: Fixture) -> None:
    result = await discover(js_hosts.seed, _options())
    found = {stat.host for stat in result.hosts}
    assert found == {BLOG_IP}, f"a plain fetch should see one host, saw {found}"
    assert result.rendered_pages == 0


# ── the three routes ─────────────────────────────────────────────────────


@needs_browser
async def test_rendering_finds_every_host_a_fetch_cannot(js_hosts: Fixture) -> None:
    result = await discover(js_hosts.seed, _options(use_browser=True))
    found = {stat.host for stat in result.hosts}
    assert PIXEL_IP in found, "the network log should catch a host only a script names"
    assert LINKED_IP in found, "a page behind a script-injected link should be sampled"
    assert SCROLLED_IP in found, "a page behind infinite scroll should be sampled"
    assert result.rendered_pages >= 4
    assert result.browser_requests > 0


@needs_browser
async def test_only_the_javascript_host_is_reported_as_browser_only(js_hosts: Fixture) -> None:
    """The distinction the report has to get right.

    Three hosts are found only with a browser, but only one of them is absent
    from every page's HTML. The other two are ordinary `<img>` tags on pages
    that were merely out of reach — so calling all three "invisible to markup"
    would overstate what rendering did.
    """
    result = await discover(js_hosts.seed, _options(use_browser=True))
    assert result.browser_only_hosts == [PIXEL_IP]
    assert any(PIXEL_IP in warning for warning in result.warnings)


@needs_browser
async def test_the_dom_alone_would_miss_the_javascript_host(js_hosts: Fixture) -> None:
    """Why the network log is the evidence and the rendered DOM is not.

    `new Image()` fetches without ever entering the document, so re-parsing
    the rendered page — the obvious implementation — finds nothing. This is
    the measurement the module docstring rests on, kept as a test because the
    tempting simplification is to drop the request listener and parse the DOM.
    """
    from cairn.services.htmlrefs import host_of, parse_page

    async with render.Renderer(scroll_passes=2) as renderer:
        page = await renderer.get(js_hosts.seed)

    in_dom = {host_of(url) for url in parse_page(page.html, js_hosts.seed).assets}
    in_log = {host_of(request.url) for request in page.requests}
    assert PIXEL_IP not in in_dom
    assert PIXEL_IP in in_log


@needs_browser
async def test_a_site_that_does_not_need_a_browser_is_told_so(js_hosts: Fixture) -> None:
    """The other half of the report, and the one that saves the next hour."""
    result = await discover(js_hosts.plain_seed, _options(use_browser=True))
    assert result.browser_only_hosts == []
    assert any("no host the HTML did not already name" in w for w in result.warnings)


# ── the parts that need no browser ───────────────────────────────────────


def test_a_browser_run_is_capped_and_says_so() -> None:
    capped, note = render.clamp_pages(500)
    assert capped == render.BROWSER_PAGE_CEILING
    assert note and "500" in note

    unchanged, quiet = render.clamp_pages(10)
    assert unchanged == 10
    assert quiet is None


def test_a_session_cookie_survives_the_trip_into_a_browser(tmp_path: object) -> None:
    """The inverse of `to_netscape`, and the same trap in the other direction.

    Netscape writes a session cookie's expiry as 0. Handed to Chromium as 0 it
    is a cookie that expired at the epoch, dropped on the way in — leaving a
    context that looks like it has the jar and does not.
    """
    from pathlib import Path

    jar = Path(str(tmp_path)) / "cookies.txt"
    jar.write_text(
        "# Netscape HTTP Cookie File\n"
        ".example.com\tTRUE\t/\tTRUE\t0\tsession_only\tabc\n"
        ".example.com\tTRUE\t/\tFALSE\t2000000000\tlasting\tdef\n",
        encoding="utf-8",
    )

    state = browser.storage_state_from_jar(str(jar))
    assert state is not None
    by_name = {cookie["name"]: cookie for cookie in state["cookies"]}
    assert by_name["session_only"]["expires"] == -1
    assert by_name["lasting"]["expires"] == 2000000000
    assert by_name["session_only"]["secure"] is True


def test_no_jar_is_not_an_error() -> None:
    assert browser.storage_state_from_jar(None) is None
