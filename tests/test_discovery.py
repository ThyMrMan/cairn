"""Discovery: parsing, fingerprinting, classification, defaults.

The end-to-end run against a Blogger-shaped fixture lives in
test_discovery_e2e.py; these are the units, especially the ones where getting
it wrong is silent — a sitemap that parses to nothing, a host group that
merges two people's blogs, a default that turns something on by accident.
"""

from __future__ import annotations

import re

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
    BLOGGER_ARCHIVE_PAGER_REJECT,
    BLOGGER_LEAN,
    BLOGGER_LEAN_PRESET,
    BLOGGER_PAGER_REJECT,
    BLOGGER_PRESET,
    DISCOURSE,
    DISCOURSE_PRESET,
    MEDIAWIKI,
    MEDIAWIKI_PRESET,
    PRESETS,
    SQUARESPACE,
    SQUARESPACE_PRESET,
    WORDPRESS,
    Preset,
    fingerprint,
    matches_host_pattern,
)
from cairn.discovery.sources import parse_feed, parse_sitemap
from cairn.services.htmlrefs import parse_page
from cairn.services.scope import (
    HostRule,
    Scope,
    asset_only_reject_pattern,
    build_reject_patterns,
    combine_patterns,
)

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


def test_every_platform_we_can_detect_has_a_preset() -> None:
    """Detection without a preset is worse than no detection.

    Squarespace shipped in the generator hints with no entry in PRESETS, so a
    Squarespace site fingerprinted with *strong* confidence and then offered
    nothing: `Fingerprint.preset` was None, the "apply the … preset" button
    never rendered, and the platform showed as the bare id because the display
    name comes from the preset. docs/04 said it enabled one.

    Recognising a platform is a promise that something follows from it.
    """
    from cairn.discovery.platform import _BODY_HINTS, _GENERATOR_HINTS, _HOST_HINTS

    detectable = {
        platform for table in (_GENERATOR_HINTS, _HOST_HINTS, _BODY_HINTS) for platform, _ in table
    }
    assert detectable <= set(PRESETS), (
        f"detected but no preset: {sorted(detectable - set(PRESETS))}"
    )


def test_squarespace_is_recognised_three_ways() -> None:
    """Generator, hostname and markup, because the custom-domain case has
    nothing in the hostname and is the one worth presetting."""
    by_generator = fingerprint(url="https://example.com/", generator="Squarespace 7.1")
    assert by_generator.platform == SQUARESPACE
    assert by_generator.confidence == "strong"
    assert by_generator.preset is SQUARESPACE_PRESET

    assert fingerprint(url="https://studio.squarespace.com/").platform == SQUARESPACE

    body = b'<html><script>Static.SQUARESPACE_CONTEXT = {"website":{}}</script></html>'
    by_body = fingerprint(url="https://owndomain.example/", body=body)
    assert by_body.platform == SQUARESPACE
    assert by_body.confidence == "weak"


def test_mediawiki_is_recognised_from_the_generator_hostname_and_markup() -> None:
    by_generator = fingerprint(url="https://wiki.example/", generator="MediaWiki 1.41.0")
    assert by_generator.platform == MEDIAWIKI
    assert by_generator.preset is MEDIAWIKI_PRESET

    # Wikimedia projects, Fandom and the wiki farms, which is most of what
    # anybody wants a copy of.
    for host in (
        "https://en.wikipedia.org/wiki/Harris",
        "https://commons.wikimedia.org/",
        "https://fallout.fandom.com/",
        "https://somewiki.miraheze.org/",
    ):
        assert fingerprint(url=host).platform == MEDIAWIKI, host

    # A self-hosted wiki on its own domain has nothing in the hostname.
    body = b'<html><script>RLCONF={"wgPageName":"Harris"};</script></html>'
    assert fingerprint(url="https://wiki.owndomain.example/", body=body).platform == MEDIAWIKI


def test_discourse_is_recognised_from_the_generator_and_markup() -> None:
    by_generator = fingerprint(url="https://forum.example/", generator="Discourse 3.2.0")
    assert by_generator.platform == DISCOURSE
    assert by_generator.preset is DISCOURSE_PRESET

    # Discourse is nearly always on a custom domain, so the markup is the
    # path that matters — there is no useful hostname pattern to add.
    body = b'<html><link href="https://sea1.discourse-cdn.com/x/stylesheets/a.css"></html>'
    result = fingerprint(url="https://forum.owndomain.example/", body=body)
    assert result.platform == DISCOURSE
    assert result.confidence == "weak"


def test_the_squarespace_preset_rejects_the_json_twin_and_nothing_else() -> None:
    """?format=json is the whole page again; ?offset= is the site's pagination.

    The second half is the Blogger lesson: rejecting a blog's own Older-posts
    trail saved nothing worth having and left a dead link in the archive. The
    equivalent here must survive the preset.
    """
    import re

    patterns = [re.compile(p) for p, _note in SQUARESPACE_PRESET.reject_patterns]

    def rejected(url: str) -> bool:
        return any(p.search(url) for p in patterns)

    assert rejected("https://site.example/about?format=json")
    assert rejected("https://site.example/about?format=json-pretty")
    # Pagination, filtering, and the image CDN's width variants all survive.
    assert not rejected("https://site.example/blog?offset=1700000000000")
    assert not rejected("https://site.example/blog?tag=travel")
    assert not rejected("https://images.squarespace-cdn.com/content/v1/a/b/x.jpg?format=2500w")
    # The RSS feed the preset itself goes looking for must not be rejected by it.
    for path in SQUARESPACE_PRESET.feed_paths:
        assert not rejected(f"https://site.example{path}")


def _rejects(preset: Preset, url: str) -> bool:
    import re

    return any(re.search(pattern, url) for pattern, _note in preset.reject_patterns)


# ── the content warning, rejected by default ─────────────────────────────
#
# Blogger sends the whole post and frames a gate over it. The reject cannot
# stop the frame document — that is a page navigation, which block rules
# exempt — but it starves the gate of sub-resources (~500 KB down to ~80 KB
# each) and keeps it out of replay's index, which is what `withheld_patterns`
# reads. Measured on one capture: 144 of these from www.blogger.com and 5 more
# from draft.blogger.com, for the same gate.


@pytest.mark.parametrize(
    "url",
    [
        "https://www.blogger.com/interstitial/blog?u=https://example.blogspot.com/",
        "https://www.blogger.com/interstitial/blog?u=https://example.blogspot.com/p.html",
        # The subdomain that also served it, and the reason the host is not
        # spelled out as `www`.
        "https://draft.blogger.com/interstitial/blog?u=https://example.blogspot.com/",
        "http://www.blogger.com/interstitial/blog",
    ],
)
def test_the_blogger_preset_rejects_the_content_warning_by_default(url: str) -> None:
    assert _rejects(BLOGGER_PRESET, url)


@pytest.mark.parametrize(
    "url",
    [
        # A post whose slug happens to contain the word. Anchoring to the
        # blogger.com host is what keeps the blog's own content out of it.
        "https://example.blogspot.com/2026/08/the-interstitial-problem.html",
        "https://example.blogspot.com/interstitial/",
        # Not the gate: the widget script the preset deliberately keeps.
        "https://www.blogger.com/static/v1/widgets/123-widgets.js",
    ],
)
def test_the_content_warning_reject_does_not_reach_the_blog_itself(url: str) -> None:
    assert not _rejects(BLOGGER_PRESET, url)


def test_the_lean_variant_inherits_the_content_warning_reject() -> None:
    """It composes from the standard preset, and this pins that it still does."""
    assert _rejects(BLOGGER_LEAN_PRESET, "https://www.blogger.com/interstitial/blog?u=x")


def test_the_reject_reaches_replays_withholding_not_just_the_crawl() -> None:
    """The pattern's second job, and the one that survives a page navigation.

    `withheld_patterns` matches a CDXJ record's `url` field, so a pattern that
    only ever made sense against a crawl queue would leave the gate replayable
    while looking like it had been dealt with.
    """
    import re

    from cairn.services.replay import _without

    line = (
        "com,blogger,www)/interstitial/blog?u=https://example.blogspot.com/ 20260816135725 "
        '{"url": "https://www.blogger.com/interstitial/blog?u=https://example.blogspot.com/", '
        '"mime": "text/html", "status": "200"}'
    )
    post = (
        "com,blogspot,example)/2026/08/post.html 20260816135725 "
        '{"url": "https://example.blogspot.com/2026/08/post.html", '
        '"mime": "text/html", "status": "200"}'
    )
    patterns = [p for p, _note in BLOGGER_PRESET.reject_patterns if "interstitial" in p]
    assert patterns, "the preset no longer carries an interstitial reject"

    kept, withheld = _without([line, post], patterns)
    assert withheld == 1
    assert kept == [post]
    # And the SURT key is host-reversed, so a pattern matched against the key
    # rather than the url would never have fired.
    assert not re.search(patterns[0], "com,blogger,www)/interstitial/blog")


@pytest.mark.parametrize(
    "url",
    [
        # The two unbounded ones. A wiki with N revisions of an article offers
        # on the order of N² diffs, which is the classic way a crawl of a wiki
        # never finishes.
        "https://wiki.example/w/index.php?title=Harris&diff=849&oldid=848",
        "https://wiki.example/w/index.php?title=Harris&oldid=848",
        "https://wiki.example/wiki/Special:Random",
        # One per article, every article.
        "https://wiki.example/w/index.php?title=Harris&action=edit",
        "https://wiki.example/w/index.php?title=Harris&action=edit&section=3",
        "https://wiki.example/w/index.php?title=Harris&action=history",
        "https://wiki.example/wiki/Harris?veaction=edit",
        "https://wiki.example/wiki/Harris?printable=yes",
        # The whole wiki again, once per language and once per skin.
        "https://wiki.example/wiki/Harris?uselang=fr",
        "https://wiki.example/wiki/Harris?useskin=monobook",
        "https://wiki.example/w/api.php?action=query&format=json",
    ],
)
def test_the_mediawiki_preset_rejects_the_ways_a_wiki_multiplies(url: str) -> None:
    assert _rejects(MEDIAWIKI_PRESET, url)


@pytest.mark.parametrize(
    "url",
    [
        # Articles, in both URL layouts a wiki can be configured with.
        "https://wiki.example/wiki/Harris",
        "https://wiki.example/w/index.php?title=Harris",
        # The skin's CSS and JS bundle. Rejecting this leaves every archived
        # page unstyled, which is the expensive mistake in this preset.
        "https://wiki.example/w/load.php?modules=startup&only=scripts",
        "https://wiki.example/w/load.php?modules=site.styles&only=styles",
        # Site CSS on a wiki old enough to serve it this way — the reason
        # action=raw is not in the reject list.
        "https://wiki.example/w/index.php?title=MediaWiki:Common.css&action=raw&ctype=text/css",
        # Index pages are how a wiki is enumerated at all.
        "https://wiki.example/wiki/Special:AllPages",
        # Namespaces are a per-site decision, not the preset's.
        "https://wiki.example/wiki/Talk:Harris",
        "https://wiki.example/wiki/Category:Islands",
        "https://wiki.example/wiki/File:Harris.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/a/ab/Harris.jpg",
    ],
)
def test_the_mediawiki_preset_leaves_content_and_styling_alone(url: str) -> None:
    assert not _rejects(MEDIAWIKI_PRESET, url)


@pytest.mark.parametrize(
    "url",
    [
        # Under a browser engine this one does not stop on its own.
        "https://forum.example/message-bus/a1b2c3/poll?dlp=t",
        "https://forum.example/session/csrf",
        "https://forum.example/admin/dashboard",
        "https://forum.example/u/hazel/notifications",
        "https://forum.example/u/hazel/preferences",
        "https://forum.example/search?q=harris",
        "https://forum.example/latest?order=activity&ascending=false",
        "https://forum.example/assets/app.js?_=1755000000",
        "https://forum.example/email/unsubscribe/9f3a2b",
    ],
)
def test_the_discourse_preset_rejects_what_cannot_be_archived(url: str) -> None:
    assert _rejects(DISCOURSE_PRESET, url)


@pytest.mark.parametrize(
    "url",
    [
        "https://forum.example/t/the-ferry-to-tarbert/123",
        # Pagination *within* a topic is more posts, not the same page again.
        "https://forum.example/t/the-ferry-to-tarbert/123?page=2",
        "https://forum.example/c/general/5",
        "https://forum.example/latest",
        # `/uploads/` and `/user_avatar/` both start with `/u`, and the
        # per-user reject is one careless `[^/]*` away from eating every image
        # on the forum.
        "https://forum.example/uploads/default/original/1X/abc123.png",
        "https://forum.example/user_avatar/forum.example/hazel/45/123_2.png",
        # A profile page is content; its notification inbox is not.
        "https://forum.example/u/hazel",
    ],
)
def test_the_discourse_preset_leaves_the_forum_alone(url: str) -> None:
    assert not _rejects(DISCOURSE_PRESET, url)


def test_no_preset_rejects_the_feed_or_sitemap_it_goes_looking_for() -> None:
    """A preset that blocks its own discovery paths is silently self-defeating.

    Cheap to get wrong — Discourse's `/latest.rss` sits one careless pattern
    away from the `?order=` reject, and it would fail by finding nothing
    rather than by erroring.
    """
    for preset in PRESETS.values():
        for path in (*preset.feed_paths, *preset.sitemap_paths):
            url = f"https://site.example{path}"
            assert not _rejects(preset, url), f"{preset.id} rejects its own {path}"


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
    # www.blogger.com is assets-on but never crawlable: it serves the theme's
    # widgets.js, and its two worthless paths are rejected by pattern instead.
    assert by_host["www.blogger.com"].fetch_assets
    assert not by_host["www.blogger.com"].crawl_pages


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


def test_blogger_skin_images_survive_the_assets_only_reject() -> None:
    """themes.googleusercontent.com serves every skin image as `image?id=…`,
    with no file extension anywhere in the URL. Without `allow_extensionless`
    the generated reject drops all of them, and the only symptom is a page
    that renders without its background."""
    hosts = classify(
        seed_host="example.blogspot.com",
        link_refs={"example.blogspot.com": 5},
        asset_refs={"themes.googleusercontent.com": 24},
        urls_by_host={},
    )
    apply_defaults(hosts, BLOGGER_PRESET)
    skin = next(h for h in hosts if h.host == "themes.googleusercontent.com")
    assert skin.fetch_assets
    assert skin.allow_extensionless

    rule = HostRule(host=skin.host, fetch_assets=True, allow_extensionless=skin.allow_extensionless)
    pattern = asset_only_reject_pattern(rule)
    assert not re.search(pattern, "https://themes.googleusercontent.com/image?id=L1lcAxxz")


def test_blogger_com_is_split_rather_than_dropped_wholesale() -> None:
    """It serves the theme's own widgets.js, whose absence shows up as console
    errors in replay, alongside two things worth nothing. Turning the whole
    host off loses all three; rejecting by pattern keeps the one that matters.

    The admin CSS is the interesting reject: Blogger cache-busts it with a
    fresh `zx=` per page load, so it is a different URL every time — one extra
    fetch per page, and no two of them shareable. Two captures of the same
    blog a day apart asked for zx=a1f39d26… and zx=16cf8f57…
    """
    hosts = classify(
        seed_host="b.blogspot.com",
        link_refs={"b.blogspot.com": 5, "www.blogger.com": 14},
        asset_refs={"www.blogger.com": 12},
        urls_by_host={},
    )
    apply_defaults(hosts, BLOGGER_PRESET)
    blogger = next(h for h in hosts if h.host == "www.blogger.com")
    assert blogger.fetch_assets
    assert not blogger.crawl_pages

    scope = Scope(
        seeds=["https://b.blogspot.com/"],
        hosts=[
            HostRule("b.blogspot.com", crawl_pages=True, fetch_assets=True),
            HostRule("www.blogger.com", crawl_pages=False, fetch_assets=True),
        ],
        reject_patterns=[p for p, _note in BLOGGER_PRESET.reject_patterns],
    )
    rejects = re.compile(combine_patterns(build_reject_patterns(scope)))

    assert not rejects.search("https://www.blogger.com/static/v1/widgets/4033524873-widgets.js")
    assert rejects.search("https://www.blogger.com/dyn-css/authorization.css?targetBlogID=5&zx=a1")
    assert rejects.search(
        "https://www.blogger.com/static/v1/jsbin/2830521187-comment_from_post_iframe.js"
    )


def rejects_for(preset: Preset) -> re.Pattern[str]:
    scope = Scope(
        seeds=["https://b.blogspot.com/"],
        hosts=[HostRule("b.blogspot.com", crawl_pages=True, fetch_assets=True)],
        reject_patterns=[p for p, _note in preset.reject_patterns],
    )
    return re.compile(combine_patterns(build_reject_patterns(scope)))


def blogger_rejects() -> re.Pattern[str]:
    return rejects_for(BLOGGER_PRESET)


def test_blogger_furniture_a_browser_fetches_is_rejected() -> None:
    """Measured on a real 43-post blog captured with the browser engine.

    wget only asks for what it can see in the markup; a browser asks for
    everything the page does, and on Blogger that is a beacon, a captcha, two
    iframes and a JSONP feed — about half of every request made, and none of
    it able to do anything once the origin is gone.
    """
    rejects = blogger_rejects()
    for url in (
        "https://b.blogspot.com/b/stats?style=BLACK_TRANSPARENT&timeRange=ALL_TIME&token=x",
        "https://www.google.com/recaptcha/api2/anchor?ar=1&k=abc&size=invisible",
        "https://www.blogger.com/navbar/8912?jsh=m&origin=https://b.blogspot.com&usegapi=1",
        "https://www.blogger.com/comment/frame/8912?po=1&hl=en_GB&saa=1",
        "https://b.blogspot.com/feeds/posts/default?alt=json-in-script&callback=cb&max-results=5",
    ):
        assert rejects.search(url), url


def test_the_blogs_own_older_posts_trail_is_not_rejected() -> None:
    """Reported: a broken "Older posts" at the bottom of every archived page.

    `/search?updated-max=` is Blogger's own pagination — bounded at about one
    page per five posts, and the only way to walk the archive by hand. It used
    to be rejected as an "infinite pagination loop", which it is not; what
    multiplies is the *label* form, one chain per label.
    """
    rejects = blogger_rejects()
    trail = "https://b.blogspot.com/search?updated-max=2019-12-09T22:33:00%2B01:00&max-results=5"
    assert not rejects.search(trail)
    assert not rejects.search("https://b.blogspot.com/search?updated-min=2019-01-01T00:00:00-08:00")


def test_pagination_inside_a_label_is_still_rejected() -> None:
    """The combination that actually explodes: one chain per label."""
    rejects = blogger_rejects()
    assert rejects.search(
        "https://b.blogspot.com/search/label/Recipes?updated-max=2019-12-09T22:33:00%2B01:00"
    )
    # …while the label page itself stays a robots-and-user decision.
    assert not rejects.search("https://b.blogspot.com/search/label/Recipes")


def test_the_plain_feed_and_the_posts_survive_those_rejects() -> None:
    """The two that would be silent disasters.

    `/feeds/posts/default` is the preset's own feed path — discovery reads it,
    and a pattern that caught the JSONP form plus the plain one would break
    feed watching without any error. Label pages are a *choice*, governed by
    robots.txt and the site's own setting; they must not be hard-rejected here.
    """
    rejects = blogger_rejects()
    for url in (
        "https://b.blogspot.com/feeds/posts/default",
        "https://b.blogspot.com/feeds/posts/default?max-results=25",
        "https://b.blogspot.com/2019/05/a-real-post.html",
        "https://b.blogspot.com/search/label/Recipes",
        "https://1.bp.blogspot.com/-abc/s1600/photo.jpg",
    ):
        assert not rejects.search(url), url


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


def test_applying_a_preset_retires_a_pattern_it_no_longer_stands_behind() -> None:
    """Merging in was only half of it.

    Applying a preset must never discard a pattern somebody added by hand, so
    it only ever added — which meant a *correction* could not propagate. The
    Blogger preset used to reject the blog's own Older-posts trail as an
    infinite loop; every scope that already had that pattern went on blocking
    its own pagination, with nothing to distinguish the stale rule from a
    deliberate one.
    """
    from cairn.discovery.platform import BLOGGER_PRESET
    from cairn.services.discovery_service import apply_preset_to_scope

    stale = r"/search\?updated-(max|min)="
    assert stale in BLOGGER_PRESET.retired_patterns
    scope = Scope(
        seeds=["https://b.blogspot.com/"],
        hosts=[HostRule("b.blogspot.com", crawl_pages=True, fetch_assets=True)],
        reject_patterns=[stale, r"[?&]m=1", r"^https?://mine\.example/"],
    )

    changes = apply_preset_to_scope(scope, BLOGGER_PRESET, ["b.blogspot.com"])

    assert stale not in scope.reject_patterns
    assert any("removed reject" in c for c in changes), "a removal has to be reported"
    # A hand-added pattern is not a preset's business.
    assert r"^https?://mine\.example/" in scope.reject_patterns
    # And the current rules are merged in.
    assert any("/b/stats" in p for p in scope.reject_patterns)


def test_retiring_is_idempotent_and_quiet_when_there_is_nothing_to_retire() -> None:
    from cairn.discovery.platform import BLOGGER_PRESET
    from cairn.services.discovery_service import apply_preset_to_scope

    scope = Scope(
        seeds=["https://b.blogspot.com/"],
        hosts=[HostRule("b.blogspot.com", crawl_pages=True, fetch_assets=True)],
        reject_patterns=[p for p, _ in BLOGGER_PRESET.reject_patterns],
    )
    assert apply_preset_to_scope(scope, BLOGGER_PRESET, ["b.blogspot.com"]) == []


# ── the lean Blogger variant ─────────────────────────────────────────────


def test_the_lean_preset_differs_from_the_standard_one_only_by_pagination() -> None:
    """The whole point of a variant is that the difference is legible.

    If the two drift apart on hosts or on unrelated rejects, "try the lean one
    and compare" stops measuring pagination and starts measuring everything.
    """
    standard = {p for p, _ in BLOGGER_PRESET.reject_patterns}
    lean = {p for p, _ in BLOGGER_LEAN_PRESET.reject_patterns}

    assert standard < lean, "the lean preset must be a strict superset"
    assert lean - standard == {BLOGGER_PAGER_REJECT, BLOGGER_ARCHIVE_PAGER_REJECT}
    assert BLOGGER_LEAN_PRESET.assets_on == BLOGGER_PRESET.assets_on
    assert BLOGGER_LEAN_PRESET.hosts_off == BLOGGER_PRESET.hosts_off
    assert BLOGGER_LEAN_PRESET.extensionless_ok == BLOGGER_PRESET.extensionless_ok


def test_the_lean_preset_rejects_the_trail_the_standard_one_keeps() -> None:
    standard, lean = blogger_rejects(), rejects_for(BLOGGER_LEAN_PRESET)
    trail = "https://b.blogspot.com/search?updated-max=2019-12-09T22:33:00%2B01:00&max-results=5"

    assert not standard.search(trail)
    assert lean.search(trail)
    # The older pattern required `updated-max` to come first, so this spelling —
    # which Blogger also emits — slipped through it.
    assert lean.search(
        "https://b.blogspot.com/search?max-results=7&updated-max=2019-12-09T22:33:00%2B01:00"
    )


def test_the_lean_preset_rejects_month_archive_pagination() -> None:
    """The third member of the family, which neither preset caught before.

    One chain per month, so a ten-year blog carries 120 of them.
    """
    standard, lean = blogger_rejects(), rejects_for(BLOGGER_LEAN_PRESET)
    archive = (
        "https://b.blogspot.com/2019/04/?updated-max=2019-04-09T22:33:00%2B01:00&max-results=7"
    )

    assert not standard.search(archive)
    assert lean.search(archive)
    # The month archive itself is navigation and stays.
    assert not lean.search("https://b.blogspot.com/2019/04/")


def test_the_lean_preset_still_reaches_every_post_and_its_own_feed() -> None:
    """It removes redundant *views*, never content.

    The pager patterns sit one careless character away from `/search/label/`
    and from the post URLs themselves, and both would fail silently — by
    archiving less, with no error anywhere.
    """
    lean = rejects_for(BLOGGER_LEAN_PRESET)
    for url in (
        "https://b.blogspot.com/2019/05/a-real-post.html",
        "https://b.blogspot.com/p/about.html",
        "https://b.blogspot.com/search/label/Recipes",
        "https://b.blogspot.com/2019/04/",
        "https://b.blogspot.com/feeds/posts/default",
        "https://b.blogspot.com/feeds/posts/default?start-index=501&max-results=500&alt=json",
        "https://b.blogspot.com/sitemap.xml",
        "https://1.bp.blogspot.com/-abc/s1600/photo.jpg",
    ):
        assert not lean.search(url), url


def test_switching_between_the_blogger_presets_is_a_round_trip() -> None:
    """Try the lean preset, capture, switch back — and land where you started.

    This is the whole reason `retired_patterns` does double duty. Applying a
    preset merges *in*, so without each variant retiring what the other adds,
    trying the lean one would be a one-way door: the pagination rejects would
    stay forever, indistinguishable from a deliberate choice, and the
    comparison the variant exists for could only be run once.
    """
    from cairn.services.discovery_service import apply_preset_to_scope

    hand_added = r"^https?://mine\.example/"
    scope = Scope(
        seeds=["https://b.blogspot.com/"],
        hosts=[HostRule("b.blogspot.com", crawl_pages=True, fetch_assets=True)],
        reject_patterns=[p for p, _ in BLOGGER_PRESET.reject_patterns] + [hand_added],
    )
    before = sorted(scope.reject_patterns)

    apply_preset_to_scope(scope, BLOGGER_LEAN_PRESET, ["b.blogspot.com"])
    assert rejects_for(BLOGGER_LEAN_PRESET).search(
        "https://b.blogspot.com/search?updated-max=2019-01-01T00:00:00-08:00"
    )
    assert BLOGGER_PAGER_REJECT in scope.reject_patterns

    apply_preset_to_scope(scope, BLOGGER_PRESET, ["b.blogspot.com"])

    assert sorted(scope.reject_patterns) == before
    # And the pattern nobody's preset put there survived both passes.
    assert hand_added in scope.reject_patterns


def test_the_lean_variant_is_offered_beside_the_detected_preset() -> None:
    """Nothing fingerprints to it, so being offered is the only way in.

    A variant that can only be applied by knowing its id through the API is a
    variant nobody will ever compare against anything.
    """
    detected = fingerprint(url="https://b.blogspot.com/")

    assert detected.preset is BLOGGER_PRESET
    assert [alt.id for alt in detected.alternatives] == [BLOGGER_LEAN]
    assert detected.to_dict()["alternatives"][0]["name"] == BLOGGER_LEAN_PRESET.name
    # Both directions, so the button is there to go back with.
    assert BLOGGER in BLOGGER_LEAN_PRESET.alternatives
    # And no platform detects as the variant itself.
    assert PRESETS[BLOGGER_LEAN] is BLOGGER_LEAN_PRESET
    assert fingerprint(url="https://b.blogspot.com/").platform == BLOGGER
