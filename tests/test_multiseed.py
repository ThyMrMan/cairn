"""A site that starts from more than one address.

The case is a blog that moved to a custom domain, or one that spans two. The
answer docs/13 asks for is *one* site: one scope, one index, one replay
collection — because two sites archiving two halves of one blog give you a
capture selector that lies about which versions of a page exist.

The fixture is two loopback addresses rather than one server with two virtual
hosts, because the whole question is whether a *second host* is enumerated,
crawled and seeded, and a Host header cannot make one address into two.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Site
from cairn.discovery.runner import DiscoveryOptions, discover
from cairn.services import discovery_service
from cairn.services import sites as site_service
from cairn.services.scope import HostRule, Scope
from tests.conftest import XHR

OLD_IP = "127.0.0.1"
NEW_IP = "127.0.0.2"

PAGE = """<!doctype html><html><head><title>{title}</title></head>
<body><h1>{title}</h1><a href="/post-{n}.html">a post</a></body></html>"""

SITEMAP = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>http://{host}/post-{n}.html</loc></url></urlset>"""


@dataclass(slots=True)
class TwoDomains:
    old: str
    new: str


def _serve(ip: str, label: str, hosts: dict[str, str]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            path = self.path.split("?")[0]
            n = "old" if ip == OLD_IP else "new"
            code = 200
            if path == "/robots.txt":
                body = f"User-agent: *\nSitemap: http://{hosts[ip]}/sitemap.xml\n".encode()
                ctype = "text/plain"
            elif path == "/sitemap.xml":
                body = SITEMAP.format(host=hosts[ip], n=n).encode()
                ctype = "application/xml"
            elif path == "/" or path.endswith(".html"):
                body = PAGE.format(title=f"{label} {path}", n=n).encode()
                ctype = "text/html"
            else:
                # 404 everything else, so the sitemap probe finds one document
                # per origin rather than one per candidate path.
                body, ctype, code = b"not found", "text/plain", 404
            self.send_response(code)
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
def two_domains() -> Iterator[TwoDomains]:
    try:
        probe = socket.socket()
        probe.bind((NEW_IP, 0))
        probe.close()
    except OSError:  # pragma: no cover — a platform without 127.0.0.0/8
        pytest.skip("this platform does not offer more than one loopback address")

    hosts: dict[str, str] = {}
    servers = []
    for ip, label in ((OLD_IP, "Old domain"), (NEW_IP, "New domain")):
        server = _serve(ip, label, hosts)
        servers.append(server)
        hosts[ip] = f"{ip}:{server.server_address[1]}"
    try:
        yield TwoDomains(old=f"http://{hosts[OLD_IP]}/", new=f"http://{hosts[NEW_IP]}/")
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


# ── the service layer ────────────────────────────────────────────────────


def _site(db: Session, settings: Settings, seed: str = "https://old.example.com/") -> Site:
    return site_service.create_site(db, settings, seed_url=seed, title="Spanning blog")


def test_an_ordinary_site_has_exactly_one_seed(db: Session, settings: Settings) -> None:
    site = _site(db, settings)
    assert site_service.all_seeds(site) == ["https://old.example.com/"]
    assert site_service.extra_seeds(site) == []


def test_adding_a_seed_makes_its_host_crawlable(db: Session, settings: Settings) -> None:
    """Otherwise the seed is refused on its first request.

    A scope that does not include the second host turns the new seed into an
    immediate rejection, which reads in the capture report exactly like a site
    that is down — so adding a seed adds the rule.
    """
    site = _site(db, settings)
    site_service.add_seed(db, settings, site, "https://new.example.com/")

    scope = site_service.resolved_scope(db, site)
    assert scope.seeds == ["https://old.example.com/", "https://new.example.com/"]
    crawlable = {rule.host for rule in scope.hosts if rule.crawl_pages}
    assert "new.example.com" in crawlable


def test_a_seed_already_marked_assets_only_becomes_crawlable(
    db: Session, settings: Settings
) -> None:
    site = _site(db, settings)
    scope = site_service.load_scope(db, site)
    scope.hosts.append(HostRule("new.example.com", crawl_pages=False, fetch_assets=True))
    site_service.save_scope(db, site, scope)

    site_service.add_seed(db, settings, site, "https://new.example.com/")
    rule = next(
        r for r in site_service.resolved_scope(db, site).hosts if r.host == "new.example.com"
    )
    assert rule.crawl_pages is True
    assert rule.fetch_assets is True


def test_saving_the_domain_picker_does_not_delete_the_other_seeds(
    db: Session, settings: Settings
) -> None:
    """The regression this design exists to prevent.

    The picker submits hosts and patterns and no seeds at all, so a scope save
    that trusted the incoming object would drop everything after the first
    address the moment anybody ticked a checkbox.
    """
    site = _site(db, settings)
    site_service.add_seed(db, settings, site, "https://new.example.com/")

    picker_submission = Scope(
        seeds=[site.seed_url],
        hosts=[HostRule("old.example.com", crawl_pages=True, fetch_assets=True)],
    )
    site_service.save_scope(db, site, picker_submission)

    assert site_service.all_seeds(site) == [
        "https://old.example.com/",
        "https://new.example.com/",
    ]


def test_saving_a_scope_does_not_forget_that_a_person_chose_it(
    db: Session, settings: Settings
) -> None:
    """The same bug in its other form.

    `user_edited` is what stops a re-index overwriting a hand-picked scope, and
    it lives in the same blob as the seeds.
    """
    site = _site(db, settings)
    site.scope_settings = {**(site.scope_settings or {}), "user_edited": True}
    site_service.save_scope(db, site, site_service.load_scope(db, site))
    assert site.scope_settings["user_edited"] is True


def test_the_first_seed_cannot_be_removed(db: Session, settings: Settings) -> None:
    site = _site(db, settings)
    with pytest.raises(site_service.SiteError, match="cannot be removed"):
        site_service.remove_seed(db, settings, site, site.seed_url)


def test_a_seed_belonging_to_another_site_is_refused(db: Session, settings: Settings) -> None:
    """Two sites archiving one URL each hold half its history."""
    _site(db, settings)
    other = site_service.create_site(db, settings, seed_url="https://other.example.com/")
    with pytest.raises(site_service.SiteError, match="already the seed"):
        site_service.add_seed(db, settings, other, "https://old.example.com/")


def test_a_duplicate_seed_is_refused(db: Session, settings: Settings) -> None:
    site = _site(db, settings)
    site_service.add_seed(db, settings, site, "https://new.example.com/")
    with pytest.raises(site_service.SiteError, match="already a seed"):
        site_service.add_seed(db, settings, site, "https://new.example.com")


def test_every_seed_reaches_the_crawler(db: Session, settings: Settings) -> None:
    site = _site(db, settings)
    site_service.add_seed(db, settings, site, "https://new.example.com/")

    scope = site_service.resolved_scope(db, site)
    seeds, counts = discovery_service.seeds_for_capture(db, site, scope)
    assert seeds[:2] == ["https://old.example.com/", "https://new.example.com/"]
    assert counts["manual"] == 2


# ── discovery over two domains ───────────────────────────────────────────


async def test_discovery_reads_the_second_domain_too(two_domains: TwoDomains) -> None:
    """Each origin has its own robots.txt and its own sitemap.

    Enumerating the second domain against the first one's map of itself would
    archive a site's new home from a list of its old one's pages.
    """
    result = await discover(
        two_domains.old,
        DiscoveryOptions(max_pages=8, max_depth=2, extra_seeds=[two_domains.new]),
    )
    hosts = {stat.host for stat in result.hosts}
    assert hosts >= {OLD_IP, NEW_IP}
    assert result.seed_hosts == [OLD_IP, NEW_IP]
    # Two sitemaps, one per origin, and a URL out of each.
    assert len(result.sitemaps) == 2
    assert any("post-old" in url for url in result.sitemap_urls)
    assert any("post-new" in url for url in result.sitemap_urls)


async def test_without_the_second_seed_the_second_domain_is_unknown(
    two_domains: TwoDomains,
) -> None:
    """The control. One seed sees one domain, however many the site has."""
    result = await discover(two_domains.old, DiscoveryOptions(max_pages=8, max_depth=2))
    assert {stat.host for stat in result.hosts} == {OLD_IP}
    assert len(result.sitemaps) == 1


async def test_both_domains_are_crawlable_in_the_scope_discovery_builds(
    two_domains: TwoDomains,
) -> None:
    result = await discover(
        two_domains.old,
        DiscoveryOptions(max_pages=8, max_depth=2, extra_seeds=[two_domains.new]),
    )
    scope = discovery_service.scope_from_result(
        result, seed_url=two_domains.old, extra_seeds=[two_domains.new]
    )
    crawlable = {rule.host for rule in scope.hosts if rule.crawl_pages}
    assert crawlable >= {OLD_IP, NEW_IP}


# ── through the API ──────────────────────────────────────────────────────


def test_seeds_round_trip_through_the_api(authed: TestClient) -> None:
    created = authed.post("/api/sites", json={"seed_url": "https://span.example.com/"}, headers=XHR)
    assert created.status_code == 201, created.text
    site_id = created.json()["id"]

    added = authed.post(f"/api/sites/{site_id}/seeds", json={"url": "old.example.net"}, headers=XHR)
    assert added.status_code == 201, added.text
    assert added.json()["seeds"] == [
        "https://span.example.com/",
        "https://old.example.net/",
    ]

    listed = authed.get(f"/api/sites/{site_id}/seeds", headers=XHR).json()
    assert listed["primary"] == "https://span.example.com/"
    assert len(listed["seeds"]) == 2

    scope = authed.get(f"/api/sites/{site_id}/scope", headers=XHR).json()
    assert scope["seeds"] == listed["seeds"]

    removed = authed.delete(f"/api/sites/{site_id}/seeds?url=https://old.example.net/", headers=XHR)
    assert removed.status_code == 200, removed.text
    assert authed.get(f"/api/sites/{site_id}/seeds", headers=XHR).json()["seeds"] == [
        "https://span.example.com/"
    ]


def test_a_nonsense_seed_is_rejected_with_a_reason(authed: TestClient) -> None:
    created = authed.post(
        "/api/sites", json={"seed_url": "https://span2.example.com/"}, headers=XHR
    )
    site_id = created.json()["id"]
    response = authed.post(f"/api/sites/{site_id}/seeds", json={"url": "ftp://nope"}, headers=XHR)
    assert response.status_code == 422
    assert "ftp" in response.json()["error"]["message"]
