"""A capture of listed pages takes those pages, and no other page.

Reported from a live instance: sixteen feed captures of one Blogger blog, 42
minutes each, 97 to 98% of every one a repeat of the last. The job said
`max_depth: 1`, and one level from a Blogger post is every monthly archive and
label in its sidebar — 83 pages — after which `--page-requisites` fetched every
image on each of them: about 1,560 a capture, to add one post.

The site here has that shape at a size a test can afford. A post links its own
image in full size (as Blogger does), a file, the post before it, and a
sidebar of archive and label pages full of images on a separate image host.
What wget asked for is read off the server, filtered to the capture's own user
agent, so the feed poller's requests are not counted as the crawl's.

Needs wget, so it runs in the container and in CI and skips elsewhere — the
same arrangement as the capture and feed suites.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, Site
from cairn.services import sites as site_service
from cairn.services.scope import HostRule
from tests.conftest import XHR

needs_wget = pytest.mark.skipif(shutil.which("wget") is None, reason="needs wget")

AGENT = "cairn-depth-test"
ARCHIVES = ("/2026/07/", "/2026/08/", "/2026/09/")
LABELS = ("/search/label/swap", "/search/label/magic")
LISTINGS = ("/", *ARCHIVES, *LABELS)
BLOG_HOST = "127.0.0.1"
CDN_HOST = "localhost"


class ShapedBlog:
    """A blog at 127.0.0.1 with its images on `localhost`, which wget treats
    as a second host. One server answers for both, by the Host header."""

    def __init__(self) -> None:
        self.blog = ""
        self.cdn = ""
        self.posts = ["older-post"]
        self._seen: list[str] = []
        self._lock = threading.Lock()

    def publish(self, slug: str) -> None:
        self.posts.append(slug)

    def post_url(self, slug: str) -> str:
        return f"{self.blog}/2026/09/{slug}.html"

    def fetched(self) -> set[str]:
        """Everything the crawl asked for, as `host/path`."""
        with self._lock:
            return set(self._seen)

    def record(self, agent: str, host: str, path: str) -> None:
        if agent == AGENT:
            with self._lock:
                self._seen.append(f"{host}{path}")

    # ── what it serves ───────────────────────────────────────────────────

    def _head(self) -> str:
        return (
            "<link rel='stylesheet' href='/theme.css'>"
            "<link rel='alternate' type='application/atom+xml' href='/feeds/posts/default'>"
            "<link rel='icon' href='/favicon.ico'>"
            "<script src='/widgets.js'></script>"
        )

    def _sidebar(self) -> str:
        links = "".join(f"<a href='{path}'>{path}</a>" for path in (*ARCHIVES, *LABELS))
        return f"<aside>{links}<a href='/'>Home</a><img src='{self.cdn}/img/a/popular=w72'></aside>"

    def post(self, slug: str) -> bytes | None:
        if slug not in self.posts:
            return None
        index = self.posts.index(slug)
        older = f"<a href='/2026/09/{self.posts[index - 1]}.html'>Older Post</a>" if index else ""
        return (
            f"<html><head>{self._head()}</head><body><h1>{slug}</h1>"
            # Blogger's shape: a thumbnail, linked to the full-size original.
            f"<a href='{self.cdn}/img/a/{slug}'><img src='{self.cdn}/img/a/{slug}=w250'></a>"
            f"<a href='/files/{slug}.pdf'>the attachment</a>"
            # An image the blog serves itself, with no extension: to a crawler
            # that decides by URL, indistinguishable from a page.
            f"<img src='/counter?post={slug}'>"
            # A page on the image host. That host allows extension-less URLs,
            # so nothing can fence it, and one level is what stops a chain of
            # them — wget reads `--level=0` as no limit at all.
            f"<a href='{self.cdn}/album/{slug}'>the album</a>"
            f"{older}{self._sidebar()}</body></html>"
        ).encode()

    def listing(self, name: str) -> bytes:
        images = "".join(f"<img src='{self.cdn}/img/a/{name}-{i}=s320'>" for i in range(4))
        body = f"<body>{images}{self._sidebar()}</body>"
        return f"<html><head>{self._head()}</head>{body}</html>".encode()

    def feed(self) -> bytes:
        entries = "".join(
            f"<entry><id>urn:post:{slug}</id><title>{slug}</title>"
            f"<link rel='alternate' type='text/html' href='{self.post_url(slug)}'/>"
            f"<published>2026-09-0{i + 1}T00:00:00Z</published></entry>"
            for i, slug in enumerate(self.posts)
        )
        return (
            "<?xml version='1.0' encoding='utf-8'?>"
            f"<feed xmlns='http://www.w3.org/2005/Atom'><title>Shaped</title>{entries}</feed>"
        ).encode()

    def listing_images(self) -> set[str]:
        names = ["home", *(f"archive{i}" for i in range(len(ARCHIVES)))]
        names += [f"label{i}" for i in range(len(LABELS))]
        return {f"{CDN_HOST}/img/a/{name}-{i}=s320" for name in names for i in range(4)}

    def other_pages(self, *wanted: str) -> set[str]:
        """Every page on the blog that is not one of `wanted`."""
        pages = {f"{BLOG_HOST}{path}" for path in LISTINGS}
        pages |= {f"{BLOG_HOST}/2026/09/{slug}.html" for slug in self.posts if slug not in wanted}
        pages.add(f"{BLOG_HOST}/feeds/posts/default")
        return pages

    def robots(self) -> set[str]:
        return {f"{BLOG_HOST}/robots.txt", f"{CDN_HOST}/robots.txt"}

    def beyond_of(self, slug: str) -> set[str]:
        """The page the album links on to, one level past the post."""
        return {f"{CDN_HOST}/album/{slug}/more"}

    def refused_of(self, slug: str) -> str:
        """What depth 0 gives up, and the audit has to say it gave up."""
        return f"{BLOG_HOST}/counter?post={slug}"

    def files_of(self, slug: str) -> set[str]:
        """What a capture of this post has to have to be a capture of it."""
        return {
            f"{BLOG_HOST}/2026/09/{slug}.html",
            f"{BLOG_HOST}/theme.css",
            f"{BLOG_HOST}/bg.png",
            f"{BLOG_HOST}/favicon.ico",
            f"{BLOG_HOST}/widgets.js",
            f"{BLOG_HOST}/files/{slug}.pdf",
            f"{CDN_HOST}/img/a/{slug}=w250",
            f"{CDN_HOST}/img/a/{slug}",
            f"{CDN_HOST}/img/a/popular=w72",
            f"{CDN_HOST}/album/{slug}",
        }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    site: ShapedBlog

    def do_GET(self) -> None:
        host = (self.headers.get("Host") or "").split(":")[0]
        path = self.path.split("?")[0]
        self.site.record(self.headers.get("User-Agent") or "", host, self.path)
        if host == CDN_HOST:
            if path.startswith("/album/") and path.count("/") < 5:
                body = f"<html><a href='{path}/more'>more of the album</a></html>"
                return self._send(body.encode(), "text/html")
            if path.startswith("/img/"):
                return self._send(b"\x89PNG\r\n\x1a\n" + self.path.encode(), "image/png")
            return self._send(b"<html>not a page anyone wants</html>", "text/html")
        listings = {"/": "home"}
        listings |= {p: f"archive{i}" for i, p in enumerate(ARCHIVES)}
        listings |= {p: f"label{i}" for i, p in enumerate(LABELS)}
        if path == "/robots.txt":
            return self._send(b"User-agent: *\nAllow: /\n", "text/plain")
        if path == "/feed.xml":
            return self._send(self.site.feed(), "application/atom+xml")
        if path.startswith("/r/"):
            # A feed item that moved: the entry points here, the post is there.
            self.send_response(301)
            self.send_header("Location", self.site.post_url(path[3:]))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if path in listings:
            return self._send(self.site.listing(listings[path]), "text/html")
        if path.startswith("/2026/09/") and path.endswith(".html"):
            body = self.site.post(path[len("/2026/09/") : -len(".html")])
            if body is not None:
                return self._send(body, "text/html")
        if path == "/theme.css":
            return self._send(b"body{background:url(/bg.png)}", "text/css")
        if path == "/widgets.js":
            return self._send(b"var widgets = 1;", "application/javascript")
        if path == "/feeds/posts/default":
            return self._send(self.site.feed(), "application/atom+xml")
        if path.endswith((".png", ".ico")) or path == "/counter":
            return self._send(b"\x89PNG\r\n\x1a\n", "image/png")
        if path.endswith(".pdf"):
            return self._send(b"%PDF-1.4 attachment", "application/pdf")
        return self._send(b"<html>not found</html>", "text/html", status=404)

    def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def shaped() -> Iterator[ShapedBlog]:
    site = ShapedBlog()
    server = ThreadingHTTPServer((BLOG_HOST, 0), type("_Bound", (_Handler,), {"site": site}))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    site.blog = f"http://{BLOG_HOST}:{port}"
    site.cdn = f"http://{CDN_HOST}:{port}"
    try:
        yield site
    finally:
        server.shutdown()
        server.server_close()


def _site(authed: TestClient, db: Session, shaped: ShapedBlog) -> Site:
    """The blog as the Blogger preset would leave it, with no politeness delay."""
    site_id = authed.post(
        "/api/sites", json={"seed_url": f"{shaped.blog}/", "title": "Shaped blog"}, headers=XHR
    ).json()["id"]
    site = db.get(Site, site_id)
    assert site is not None
    scope = site_service.load_scope(db, site)
    scope.hosts = [
        HostRule(BLOG_HOST, crawl_pages=True, fetch_assets=True),
        # Blogger's image host serves its originals without an extension.
        HostRule(CDN_HOST, crawl_pages=False, fetch_assets=True, allow_extensionless=True),
    ]
    scope.politeness.update({"wait_s": 0, "random_wait": False})
    site_service.save_scope(db, site, scope)
    site.engine_config = {"user_agent": AGENT}
    db.commit()
    return site


def _succeeds(client: TestClient, job_id: int, *, timeout: float = 120.0) -> None:
    """Block until the job ends, and fail with its own words if it failed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job: dict[str, Any] = client.get(f"/api/jobs/{job_id}", headers=XHR).json()
        if job["status"] in ("ok", "failed", "cancelled", "interrupted"):
            assert job["status"] == "ok", job.get("error")
            return
        time.sleep(0.3)
    raise AssertionError(f"job {job_id} never finished")


def _manifest(settings: Settings, db: Session, site: Site) -> dict[str, Any]:
    db.expire_all()
    capture = db.scalars(
        select(Capture).where(Capture.site_id == site.id).order_by(Capture.id.desc())
    ).first()
    assert capture is not None
    manifest = settings.archives_dir / site.archive_path / "captures" / capture.dir_name
    loaded: dict[str, Any] = json.loads((manifest / "manifest.json").read_text(encoding="utf-8"))
    return loaded


@needs_wget
def test_a_feed_capture_takes_the_post_and_what_hangs_off_it_and_no_other_page(
    authed: TestClient, db: Session, settings: Settings, shaped: ShapedBlog
) -> None:
    site = _site(authed, db, shaped)
    feed_id = authed.post(
        f"/api/sites/{site.id}/feeds", json={"url": f"{shaped.blog}/feed.xml"}, headers=XHR
    ).json()["id"]
    assert authed.post(f"/api/feeds/{feed_id}/poll", headers=XHR).json()["baseline"]

    shaped.publish("new-post")
    polled = authed.post(f"/api/feeds/{feed_id}/poll", headers=XHR).json()
    assert polled["new_items"] == 1, polled
    _succeeds(authed, polled["job_ids"][0])

    fetched = shaped.fetched()
    # The post, everything it displays, and what it links to that is not a
    # page — the full-size image above all, which only an <a href> reaches.
    missing = shaped.files_of("new-post") - fetched
    assert not missing, f"the capture lacks part of its own post: {sorted(missing)}"
    # And not one other page, nor any image that only another page shows.
    beyond = shaped.other_pages("new-post") | shaped.listing_images()
    strays = fetched & (beyond | shaped.beyond_of("new-post"))
    assert not strays, f"a feed capture followed the post into the site: {sorted(strays)}"
    # Nothing else at all, bar the robots.txt each host is asked for first.
    assert fetched - shaped.robots() == shaped.files_of("new-post"), sorted(fetched)

    manifest = _manifest(settings, db, site)
    # Recorded as what ran, so the manifest can be read years from now.
    assert manifest["scope"]["max_depth"] == 0
    # The one thing depth 0 costs is said, and said as what it is rather than
    # as a gap: the image is not here, and nothing went wrong.
    stats = manifest["stats"]
    assert shaped.refused_of("new-post") not in fetched
    assert (stats["unfollowed_assets"], stats["missing_assets"]) == (1, 0), stats
    said = next(w for w in stats["warnings"] if "only the pages it was given" in w)
    assert "/counter?post=new-post" in said


@needs_wget
def test_a_job_queued_before_the_depth_was_fixed_runs_at_zero(
    authed: TestClient, db: Session, settings: Settings, shaped: ShapedBlog
) -> None:
    """The upgrade case. Feed jobs were queued with `max_depth: 1`, and one
    still in the queue when the container is replaced must not run as one.

    Its second seed is a feed entry that redirects to the post: wget ignores a
    reject rule when deciding whether to read a redirect's target, so the post
    it lands on still gets its files.
    """
    site = _site(authed, db, shaped)
    shaped.publish("first-new")
    shaped.publish("second-new")

    supervisor = authed.app.state.supervisor  # type: ignore[attr-defined]
    job = supervisor.enqueue(
        db,
        job_type="capture",
        site_id=site.id,
        spec={
            "kind": "feed",
            "extra_seeds": [shaped.post_url("first-new"), f"{shaped.blog}/r/second-new"],
            "only_extra_seeds": True,
            "max_depth": 1,
        },
    )
    db.commit()
    supervisor.notify()
    _succeeds(authed, job.id)

    fetched = shaped.fetched()
    for slug in ("first-new", "second-new"):
        missing = shaped.files_of(slug) - fetched
        assert not missing, f"{slug} is incomplete: {sorted(missing)}"
    beyond = shaped.other_pages("first-new", "second-new") | shaped.listing_images()
    beyond |= shaped.beyond_of("first-new") | shaped.beyond_of("second-new")
    strays = fetched & beyond
    assert not strays, f"the old depth was believed: {sorted(strays)}"
    assert _manifest(settings, db, site)["scope"]["max_depth"] == 0


@needs_wget
def test_a_pasted_list_archives_those_pages_and_no_other(
    authed: TestClient, db: Session, settings: Settings, shaped: ShapedBlog
) -> None:
    """Bulk import's "archive only these" set where the crawl started and
    nothing else, so each listed page was followed into its site with no
    depth limit at all."""
    site = _site(authed, db, shaped)
    shaped.publish("bookmarked")

    done = authed.post(
        "/api/import/urls", json={"text": shaped.post_url("bookmarked")}, headers=XHR
    ).json()
    assert done["updated"] == [site.id], done
    _succeeds(authed, done["jobs"][0])

    fetched = shaped.fetched()
    missing = shaped.files_of("bookmarked") - fetched
    assert not missing, f"the listed page is incomplete: {sorted(missing)}"
    beyond = shaped.other_pages("bookmarked") | shaped.listing_images()
    strays = fetched & (beyond | shaped.beyond_of("bookmarked"))
    assert not strays, f"a pasted URL was crawled from: {sorted(strays)}"
    assert _manifest(settings, db, site)["scope"]["max_depth"] == 0
