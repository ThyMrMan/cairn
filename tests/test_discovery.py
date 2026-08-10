"""Discovery: parsing, fingerprinting, classification, defaults.

The end-to-end run against a Blogger-shaped fixture lives in
test_discovery_e2e.py; these are the units, especially the ones where getting
it wrong is silent — a sitemap that parses to nothing, a host group that
merges two people's blogs, a default that turns something on by accident.
"""

from __future__ import annotations

import pytest

from cairn.discovery.hosts import (
    ROLE_ANALYTICS,
    ROLE_IMAGES,
    ROLE_SELF,
    HostStat,
    apply_defaults,
    classify,
    registrable_domain,
)
from cairn.discovery.platform import (
    BLOGGER,
    BLOGGER_PRESET,
    WORDPRESS,
    fingerprint,
    matches_host_pattern,
)
from cairn.discovery.sources import parse_feed, parse_sitemap
from cairn.services.htmlrefs import parse_page

# ── sitemaps ─────────────────────────────────────────────────────────────

URLSET = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://blog.example.com/a.html</loc><lastmod>2026-01-02</lastmod></url>
  <url><loc>https://blog.example.com/b.html</loc></url>
</urlset>"""

SITEMAP_INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://blog.example.com/sitemap-posts.xml</loc></sitemap>
  <sitemap><loc>https://blog.example.com/sitemap-pages.xml</loc></sitemap>
</sitemapindex>"""


def test_parses_a_urlset_with_lastmod() -> None:
    urls, children = parse_sitemap(URLSET)
    assert [u for u, _ in urls] == [
        "https://blog.example.com/a.html",
        "https://blog.example.com/b.html",
    ]
    assert urls[0][1] == "2026-01-02"
    assert children == []


def test_parses_a_sitemap_index() -> None:
    urls, children = parse_sitemap(SITEMAP_INDEX)
    assert urls == []
    assert len(children) == 2


def test_parses_a_sitemap_with_no_namespace() -> None:
    """Plenty of sites emit one; refusing it loses the whole URL list."""
    urls, _ = parse_sitemap(b"<urlset><url><loc>https://a.example/x</loc></url></urlset>")
    assert [u for u, _ in urls] == ["https://a.example/x"]


def test_rejects_an_entity_expansion_attack() -> None:
    """Sitemaps are attacker-controlled XML and the stdlib parser expands
    entities by default. defusedxml must refuse rather than allocate (docs/11)."""
    bomb = b"""<?xml version="1.0"?>
    <!DOCTYPE lolz [
      <!ENTITY lol "lol">
      <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
      <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
    ]>
    <urlset><url><loc>&lol3;</loc></url></urlset>"""
    with pytest.raises(ValueError, match="not parseable"):
        parse_sitemap(bomb)


def test_rejects_an_external_entity_reference() -> None:
    """The file-disclosure variant of the same problem."""
    xxe = b"""<?xml version="1.0"?>
    <!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
    <urlset><url><loc>&xxe;</loc></url></urlset>"""
    with pytest.raises(ValueError, match="not parseable"):
        parse_sitemap(xxe)


def test_an_html_error_page_is_not_an_empty_sitemap() -> None:
    """Servers routinely answer a missing sitemap with 200 and an HTML page.

    That parses as well-formed XML and yields no <url> elements, so treating
    it as an empty sitemap is indistinguishable from a site that genuinely has
    none — and the user is told nothing either way.
    """
    with pytest.raises(ValueError, match="root element is <html>"):
        parse_sitemap(b"<html><body>this is a 404 page</body></html>")


def test_an_empty_urlset_is_empty_not_an_error() -> None:
    urls, children = parse_sitemap(b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>')
    assert urls == []
    assert children == []


def test_malformed_xml_is_a_value_error_not_a_crash() -> None:
    with pytest.raises(ValueError, match="not parseable"):
        parse_sitemap(b"<urlset><url><loc>unclosed")


# ── feeds ────────────────────────────────────────────────────────────────

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Blog</title>
  <entry><title>One</title><link rel="alternate" href="https://blog.example.com/one.html"/></entry>
  <entry><title>Two</title><link rel="alternate" href="https://blog.example.com/two.html"/></entry>
</feed>"""

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Example</title>
<item><title>One</title><link>https://blog.example.com/one.html</link></item>
</channel></rss>"""


def test_parses_atom_link_href() -> None:
    """Atom puts the URL in an attribute, not element text — Blogger's default
    format, and the ArchiveBox gap from the original evaluation."""
    result = parse_feed(ATOM, "https://blog.example.com/feeds/posts/default")
    assert result.entries == [
        "https://blog.example.com/one.html",
        "https://blog.example.com/two.html",
    ]
    assert result.title == "Example Blog"


def test_parses_rss() -> None:
    result = parse_feed(RSS, "https://blog.example.com/rss")
    assert result.entries == ["https://blog.example.com/one.html"]


def test_a_non_feed_reports_an_error_rather_than_looking_empty() -> None:
    result = parse_feed(b"<html><body>not a feed</body></html>", "https://x/")
    assert not result.ok


# ── fingerprinting ───────────────────────────────────────────────────────


def test_generator_tag_wins() -> None:
    result = fingerprint(url="https://example.com/", generator="Blogger")
    assert result.platform == BLOGGER
    assert result.confidence == "strong"
    assert result.preset is BLOGGER_PRESET


def test_blogspot_hostname_is_recognised() -> None:
    assert fingerprint(url="https://foo.blogspot.com/").platform == BLOGGER


def test_country_blogspot_domains_are_recognised() -> None:
    """Blogger redirects to ccTLDs like blogspot.co.uk."""
    assert fingerprint(url="https://foo.blogspot.co.uk/").platform == BLOGGER


def test_custom_domain_blogger_is_caught_by_the_body() -> None:
    """A Blogger blog on its own domain has nothing in the hostname, and those
    are exactly the blogs most worth presetting."""
    body = b'<html><head><script src="https://resources.blogblog.com/x.js"></script></head></html>'
    result = fingerprint(url="https://myblog.example/", body=body)
    assert result.platform == BLOGGER
    assert result.confidence == "weak"


def test_wordpress_from_markup() -> None:
    body = b'<html><link href="/wp-content/themes/x/style.css"></html>'
    assert fingerprint(url="https://example.com/", body=body).platform == WORDPRESS


def test_unknown_stays_unknown() -> None:
    result = fingerprint(url="https://example.com/", body=b"<html>hello</html>")
    assert result.platform == "unknown"
    assert result.preset is None


# ── host patterns ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("pattern", "host", "expected"),
    [
        ("*.bp.blogspot.com", "1.bp.blogspot.com", True),
        ("*.bp.blogspot.com", "4.bp.blogspot.com", True),
        ("*.bp.blogspot.com", "bp.blogspot.com", True),
        ("*.bp.blogspot.com", "evil-bp.blogspot.com", False),
        ("www.blogger.com", "www.blogger.com", True),
        ("www.blogger.com", "blogger.com", False),
        # A pattern must not match a host that merely ends with the same text.
        ("*.wp.com", "notwp.com", False),
    ],
)
def test_host_pattern_matching(pattern: str, host: str, expected: bool) -> None:
    assert matches_host_pattern(pattern, host) is expected


# ── grouping ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("example.co.uk", "example.co.uk"),
        ("www.example.com", "example.com"),
        # Multi-tenant suffixes: two people's blogs must not group together.
        ("foo.blogspot.com", "foo.blogspot.com"),
        ("bar.blogspot.com", "bar.blogspot.com"),
        ("myblog.github.io", "myblog.github.io"),
        # The image CDN groups as one, which is what the picker should show.
        ("1.bp.blogspot.com", "bp.blogspot.com"),
        ("4.bp.blogspot.com", "bp.blogspot.com"),
    ],
)
def test_registrable_domain(host: str, expected: str) -> None:
    assert registrable_domain(host) == expected


def test_neighbouring_blogs_do_not_share_a_group() -> None:
    """The ArchiveBox failure, stated as a property: a sidebar link to another
    blogspot blog must never look like part of the same site."""
    assert registrable_domain("mine.blogspot.com") != registrable_domain("theirs.blogspot.com")


# ── classification and defaults ──────────────────────────────────────────


def blogger_hosts() -> list[HostStat]:
    return classify(
        seed_host="example.blogspot.com",
        link_refs={"example.blogspot.com": 1834, "www.blogger.com": 42, "other.blogspot.com": 87},
        asset_refs={
            "example.blogspot.com": 210,
            "1.bp.blogspot.com": 3201,
            "www.google-analytics.com": 100,
            "fonts.gstatic.com": 96,
        },
        urls_by_host={
            "example.blogspot.com": {"https://example.blogspot.com/a"},
            "1.bp.blogspot.com": {"https://1.bp.blogspot.com/x.jpg"},
        },
        mime_by_host={"1.bp.blogspot.com": {"image/jpeg": 40}},
    )


def test_seed_host_is_first_and_marked_self() -> None:
    hosts = blogger_hosts()
    assert hosts[0].host == "example.blogspot.com"
    assert hosts[0].role == ROLE_SELF


def test_image_cdn_is_classified_from_its_mime_types() -> None:
    stat = next(h for h in blogger_hosts() if h.host == "1.bp.blogspot.com")
    assert stat.role == ROLE_IMAGES


def test_analytics_is_recognised() -> None:
    stat = next(h for h in blogger_hosts() if h.host == "www.google-analytics.com")
    assert stat.role == ROLE_ANALYTICS


def test_defaults_preselect_the_blogger_case_with_zero_clicks() -> None:
    """docs/04's stated goal for the picker."""
    hosts = apply_defaults(blogger_hosts(), BLOGGER_PRESET)
    by_host = {h.host: h for h in hosts}

    assert by_host["example.blogspot.com"].crawl_pages
    assert by_host["example.blogspot.com"].fetch_assets

    assert by_host["1.bp.blogspot.com"].fetch_assets
    assert not by_host["1.bp.blogspot.com"].crawl_pages

    assert not by_host["www.google-analytics.com"].fetch_assets
    assert not by_host["www.blogger.com"].fetch_assets


def test_a_neighbouring_blog_is_off_by_default() -> None:
    """The whole point of defaulting unknown hosts off: a crawl cannot reach a
    blog that was merely linked from a sidebar."""
    hosts = apply_defaults(blogger_hosts(), BLOGGER_PRESET)
    other = next(h for h in hosts if h.host == "other.blogspot.com")
    assert not other.crawl_pages
    assert not other.fetch_assets


def test_every_host_gets_a_reason() -> None:
    """The picker shows why something is on or off; a blank is a bug."""
    for stat in apply_defaults(blogger_hosts(), BLOGGER_PRESET):
        assert stat.reason


def test_asset_only_host_is_enabled_even_without_a_preset() -> None:
    hosts = classify(
        seed_host="example.com",
        link_refs={"example.com": 10},
        asset_refs={"cdn.example.net": 40},
        urls_by_host={},
    )
    apply_defaults(hosts, None)
    cdn = next(h for h in hosts if h.host == "cdn.example.net")
    assert cdn.fetch_assets
    assert not cdn.crawl_pages


# ── page parsing feeding discovery ───────────────────────────────────────


def test_page_parsing_finds_feeds_links_and_assets() -> None:
    body = b"""<html><head>
    <meta name="generator" content="Blogger">
    <title>My Blog</title>
    <link rel="alternate" type="application/atom+xml" href="/feeds/posts/default">
    <link rel="stylesheet" href="/style.css">
    </head><body>
    <a href="/post-1.html">one</a>
    <img src="https://1.bp.blogspot.com/pic.jpg">
    </body></html>"""
    page = parse_page(body, "https://example.blogspot.com/")

    assert page.generator == "Blogger"
    assert page.title == "My Blog"
    assert page.feeds == ["https://example.blogspot.com/feeds/posts/default"]
    assert "https://example.blogspot.com/post-1.html" in page.links
    assert "https://1.bp.blogspot.com/pic.jpg" in page.assets
    assert "https://example.blogspot.com/style.css" in page.assets


def test_base_href_is_honoured() -> None:
    body = b'<html><head><base href="https://cdn.example/app/"></head><img src="x.png"></html>'
    page = parse_page(body, "https://example.com/page.html")
    assert "https://cdn.example/app/x.png" in page.assets
