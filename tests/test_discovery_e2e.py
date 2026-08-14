"""M2's exit criterion, end to end.

Adding a Blogger blog must auto-discover `*.bp.blogspot.com`, preselect it as
assets-only, exclude analytics, apply the `?m=1` reject — and the resulting
capture must stay inside those bounds. The last clause is the one that matters:
a picker whose selections the crawler then ignores is decoration.

The fixture is a Blogger-shaped site served on three virtual hosts, so the
scope decisions have something real to be right or wrong about.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import XHR

JOB_TIMEOUT_S = 90
POLL_S = 0.2

# Hosts are distinguished by the Host header, so one server can be all three.
# The analytics host is its real name on purpose: "excludes analytics" is part
# of the exit criterion, and a made-up hostname would exercise the generic
# unknown-host rule instead of the blocklist that actually has to work.
BLOG = "blog.localhost"
IMAGES = "images.localhost"
ANALYTICS = "www.google-analytics.com"

POSTS = ["hello-world", "second-post", "third-post"]


def _page(host_port: int, slug: str, title: str) -> bytes:
    """A post page shaped like Blogger's output."""
    return f"""<html><head>
<meta name="generator" content="Blogger">
<title>{title}</title>
<link rel="alternate" type="application/atom+xml" href="/feeds/posts/default">
<link rel="stylesheet" href="/style.css">
<style>body{{background:url(https\\:\\/\\/{IMAGES}:{host_port}\\/theme.png)}}</style>
</head><body>
<h1>{title}</h1>
<p>CONTENT-{slug.upper()}</p>
<!-- Blogger's lightbox pattern: every post image is wrapped in a link to the
     full-size file on the image CDN. Those links must not make the CDN look
     like a site worth crawling. -->
<a href="http://{IMAGES}:{host_port}/s1600/{slug}.jpg">
  <img src="http://{IMAGES}:{host_port}/{slug}.jpg"></a>
<script src="http://{ANALYTICS}:{host_port}/track.js"></script>
<a href="/{slug}.html?m=1">mobile version</a>
<a href="/">home</a>
</body></html>""".encode()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    port = 0

    def do_GET(self) -> None:
        host = self.headers.get("Host", "").split(":")[0]
        path = self.path.split("?")[0]
        port = self.server.server_address[1]

        body, ctype, code = b"not found", "text/html", 404

        if host == IMAGES:
            if path.endswith(".jpg") or path.endswith(".png"):
                body, ctype, code = b"\xff\xd8\xffIMAGE", "image/jpeg", 200
            elif path == "/gallery.html":
                body, ctype, code = b"<html>LEAKED-IMAGE-HOST-PAGE</html>", "text/html", 200
        elif host == ANALYTICS:
            body, ctype, code = b"/* LEAKED-ANALYTICS */", "application/javascript", 200
        elif path == "/robots.txt":
            body, ctype, code = (
                (
                    f"User-agent: *\nDisallow: /search\nSitemap: http://{BLOG}:{port}/sitemap.xml\n"
                ).encode(),
                "text/plain",
                200,
            )
        elif path == "/sitemap.xml":
            entries = "".join(
                f"<url><loc>http://{BLOG}:{port}/{slug}.html</loc></url>" for slug in POSTS
            )
            body = (
                '<?xml version="1.0"?><urlset '
                'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                f"{entries}</urlset>"
            ).encode()
            ctype, code = "application/xml", 200
        elif path == "/feeds/posts/default":
            entries = "".join(
                f"<entry><title>{s}</title>"
                f'<link rel="alternate" href="http://{BLOG}:{port}/{s}.html"/></entry>'
                for s in POSTS
            )
            body = (
                '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
                f"<title>Fixture Blogger</title>{entries}</feed>"
            ).encode()
            ctype, code = "application/atom+xml", 200
        elif path == "/style.css":
            body, ctype, code = b"body{color:#222}", "text/css", 200
        elif path in ("/", "/index.html"):
            body, ctype, code = _page(port, "home", "Fixture Blogger"), "text/html", 200
        elif path.endswith(".html"):
            slug = path[1:-5]
            if slug in POSTS:
                body, ctype, code = (
                    _page(port, slug, slug.replace("-", " ").title()),
                    "text/html",
                    200,
                )

        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def blogger_site(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Three virtual hosts on one loopback server.

    Resolution is patched rather than relying on the machine's DNS: `*.localhost`
    resolves to loopback on Linux but not on Windows, and the whole point of
    this fixture is that the hosts are *different hostnames* — scope decisions
    are per-host, so serving them on different ports would test nothing.
    """
    import socket

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fixture_hosts = {BLOG, IMAGES, ANALYTICS}
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        # httpx's async path hands the host over as bytes while its sync path
        # uses str, so comparing without decoding silently misses every async
        # lookup — which is the only kind discovery makes.
        name = host.decode("ascii", "ignore") if isinstance(host, bytes) else host
        if name in fixture_hosts:
            return real_getaddrinfo("127.0.0.1", port, *args, **kwargs)
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    try:
        yield f"http://{BLOG}:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


def wait_for_job(client: TestClient, job_id: int) -> dict[str, Any]:
    deadline = time.monotonic() + JOB_TIMEOUT_S
    while time.monotonic() < deadline:
        job: dict[str, Any] = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(POLL_S)
    raise AssertionError(f"job {job_id} did not finish in {JOB_TIMEOUT_S}s")


def discovered_site(client: TestClient, seed: str) -> tuple[int, dict[str, Any]]:
    created = client.post("/api/sites", json={"seed_url": seed}, headers=XHR)
    assert created.status_code == 201, created.text
    site_id = created.json()["id"]

    started = client.post(f"/api/sites/{site_id}/discover", headers=XHR)
    assert started.status_code == 202, started.text
    job = wait_for_job(client, started.json()["job_id"])
    assert job["status"] == "ok", f"discovery failed: {job.get('error')!r}"

    result = client.get(f"/api/sites/{site_id}/discovery").json()
    return site_id, result


# ── the exit criterion ───────────────────────────────────────────────────


def test_discovery_finds_the_platform_and_its_sources(
    authed: TestClient, blogger_site: str
) -> None:
    _site_id, result = discovered_site(authed, blogger_site)
    summary = result["discovery"]["summary"]

    assert summary["fingerprint"]["platform"] == "blogger"
    assert summary["urls_from_sitemaps"] >= len(POSTS)
    assert summary["urls_from_feeds"] >= len(POSTS)
    assert summary["robots"]["fetched"]
    # robots.txt is recorded, not obeyed: /search is where Blogger label pages
    # live and hiding them would be a decision made behind the user's back.
    assert "/search" in summary["robots"]["disallowed"]


def test_the_picker_preselects_the_blogger_case(authed: TestClient, blogger_site: str) -> None:
    """docs/04's stated goal: zero clicks for the common case."""
    _site_id, result = discovered_site(authed, blogger_site)
    by_host = {h["host"]: h for h in result["hosts"]}

    assert by_host[BLOG]["crawl_pages"]
    assert by_host[BLOG]["fetch_assets"]

    assert by_host[IMAGES]["fetch_assets"], "the image host should be preselected"
    assert not by_host[IMAGES]["crawl_pages"], "the image host must not be crawled as a site"

    assert not by_host[ANALYTICS]["fetch_assets"]
    assert not by_host[ANALYTICS]["crawl_pages"]


def test_the_blogger_reject_patterns_are_applied(authed: TestClient, blogger_site: str) -> None:
    site_id, _result = discovered_site(authed, blogger_site)
    scope = authed.get(f"/api/sites/{site_id}/scope").json()
    assert any("m=1" in pattern for pattern in scope["reject_patterns"])


def test_the_selection_becomes_the_crawlers_actual_boundary(
    authed: TestClient, blogger_site: str
) -> None:
    """The clause that makes the picker more than decoration.

    Everything above checks what was *selected*. This checks that the selection
    survives translation into what the crawler is told: the blog crawlable, the
    image host reachable but fenced off from being crawled, and analytics not
    in the allowlist at all.
    """
    site_id, _result = discovered_site(authed, blogger_site)
    scope = authed.get(f"/api/sites/{site_id}/scope").json()
    args = " ".join(scope["wget_preview"])

    domains = next(a for a in scope["wget_preview"] if a.startswith("--domains="))
    assert BLOG in domains
    assert IMAGES in domains, "an asset host left out of --domains loses its images entirely"
    assert ANALYTICS not in domains

    # The image host is in --domains, so only the reject regex stops it being
    # crawled as a site.
    reject = next(a for a in scope["wget_preview"] if a.startswith("--reject-regex="))
    assert IMAGES.replace(".", r"\.") in reject
    assert "--regex-type=pcre" in args, "the lookahead needs PCRE; POSIX would fail at crawl time"
    assert "m=1" in reject


def test_discovered_feeds_are_attached_to_the_site(authed: TestClient, blogger_site: str) -> None:
    _site_id, result = discovered_site(authed, blogger_site)
    assert any("/feeds/posts/default" in feed for feed in result["discovery"]["summary"]["feeds"])


def test_pagination_stops_when_a_server_ignores_the_page_parameter(
    authed: TestClient, blogger_site: str
) -> None:
    """`?page=N` is a Blogger convention, not part of the sitemap spec.

    A server that has never heard of it serves page 1 again, so a loop that
    only stops on an empty response fetches the same document sixty times and
    reports sixty sitemaps. The fixture ignores the parameter, exactly like
    most servers would.
    """
    _site_id, result = discovered_site(authed, blogger_site)
    summary = result["discovery"]["summary"]
    assert len(summary["sitemaps"]) == 1, f"refetched the same sitemap: {summary['sitemaps']}"
    assert summary["urls_from_sitemaps"] == len(POSTS)


def test_scope_preview_estimates_without_fetching(authed: TestClient, blogger_site: str) -> None:
    site_id, _result = discovered_site(authed, blogger_site)
    preview = authed.post(f"/api/sites/{site_id}/scope/preview", headers=XHR).json()
    assert preview["pages_to_crawl"] >= 1
    assert BLOG in preview["crawl_hosts"]
    assert IMAGES in preview["asset_hosts"]


def test_rerunning_discovery_keeps_a_user_edited_scope(
    authed: TestClient, blogger_site: str
) -> None:
    """A deliberate selection must survive. Silently undoing it would make the
    picker feel arbitrary and cost a re-capture to notice."""
    site_id, _result = discovered_site(authed, blogger_site)

    edited = authed.put(
        f"/api/sites/{site_id}/scope",
        json={
            "hosts": [
                {"host": BLOG, "crawl_pages": True, "fetch_assets": True},
            ],
            "reject_patterns": ["[?&]m=1"],
        },
        headers=XHR,
    )
    assert edited.status_code == 200, edited.text

    started = authed.post(f"/api/sites/{site_id}/discover", headers=XHR)
    wait_for_job(authed, started.json()["job_id"])

    scope = authed.get(f"/api/sites/{site_id}/scope").json()
    assert [h["host"] for h in scope["hosts"]] == [BLOG], "the re-run overwrote a chosen scope"


def test_obey_robots_round_trips_through_the_scope_api(
    authed: TestClient, blogger_site: str
) -> None:
    """The setting the scope editor now has a control for.

    It was in the model and in both engines and nowhere in the UI, so the only
    way to change it was this call — which nothing exercised either. On Blogger
    it governs everything under /search, including the blog's own Older-posts
    trail, so advice to turn it off was advice to use the API.
    """
    site_id, _result = discovered_site(authed, blogger_site)
    assert authed.get(f"/api/sites/{site_id}/scope").json()["obey_robots"] is True

    saved = authed.put(
        f"/api/sites/{site_id}/scope",
        json={
            "hosts": [{"host": BLOG, "crawl_pages": True, "fetch_assets": True}],
            "obey_robots": False,
        },
        headers=XHR,
    )
    assert saved.status_code == 200, saved.text
    assert authed.get(f"/api/sites/{site_id}/scope").json()["obey_robots"] is False

    # And back, because a toggle that only goes one way is half a control.
    authed.put(
        f"/api/sites/{site_id}/scope",
        json={
            "hosts": [{"host": BLOG, "crawl_pages": True, "fetch_assets": True}],
            "obey_robots": True,
        },
        headers=XHR,
    )
    assert authed.get(f"/api/sites/{site_id}/scope").json()["obey_robots"] is True


def test_applying_a_preset_reports_what_it_changed(authed: TestClient, blogger_site: str) -> None:
    site_id, _result = discovered_site(authed, blogger_site)
    authed.put(
        f"/api/sites/{site_id}/scope",
        json={"hosts": [{"host": BLOG, "crawl_pages": True, "fetch_assets": True}]},
        headers=XHR,
    )
    applied = authed.post(
        f"/api/sites/{site_id}/scope/apply-preset", json={"preset": "blogger"}, headers=XHR
    )
    assert applied.status_code == 200, applied.text
    assert any("m=1" in note for note in applied.json()["notes"])
