"""The asset audit: what a capture referenced but does not contain.

Its whole job is to make a silent gap loud, so the cases that matter are the
ones nothing else reports — CSS-escaped URLs wget mishandles, and lazy-loaded
images it cannot see at all.
"""

from __future__ import annotations

from cairn.services.postprocess import (
    _escaped_target_host,
    _referenced_assets,
    _unescape_css,
)

# The exact shape a real Blogger skin produces, from a live capture's log.
BLOGGER_MANGLED = (
    "https://jsrandomtest29.blogspot.com/2026/08/https%5C:%5C/%5C/"
    "themes.googleusercontent.com%5C/image?id=L1lcAxxz0CLgsDzixEprHJ2F38TyEjCyE3RSAjynQDks"
)


# ── CSS escape decoding ──────────────────────────────────────────────────


def test_unescapes_the_blogger_skin_form() -> None:
    escaped = r"https\:\/\/themes.googleusercontent.com\/image?id=abc"
    assert _unescape_css(escaped) == "https://themes.googleusercontent.com/image?id=abc"


def test_unescaping_leaves_ordinary_urls_alone() -> None:
    plain = "https://example.com/a/b.png?x=1&y=2"
    assert _unescape_css(plain) == plain


# ── reference extraction ─────────────────────────────────────────────────


def test_finds_plain_css_url_references() -> None:
    body = b"<html><head><style>body{background:url(/theme.png)}</style></head></html>"
    found = _referenced_assets(body, "https://blog.example.com/post.html")
    assert "https://blog.example.com/theme.png" in found


def test_finds_the_real_target_behind_a_css_escape() -> None:
    """The reference wget turns into a 404 against the wrong host.

    Without decoding, the audit would either miss it or report the mangled
    relative path, neither of which tells anyone what is actually absent.
    """
    body = (
        rb"<html><head><style>"
        rb"body{background:url(https\:\/\/themes.googleusercontent.com\/image?id=abc)}"
        rb"</style></head></html>"
    )
    found = _referenced_assets(body, "https://blog.example.com/2026/08/post.html")
    assert "https://themes.googleusercontent.com/image?id=abc" in found
    assert not any("blogspot" in u or "\\" in u for u in found)


def test_finds_quoted_and_unquoted_css_urls() -> None:
    body = (
        b"<html><style>"
        b"a{background:url('https://a.example/1.png')}"
        b'b{background:url("https://b.example/2.png")}'
        b"c{background:url(https://c.example/3.png)}"
        b"</style></html>"
    )
    found = _referenced_assets(body, "https://blog.example.com/")
    assert {
        "https://a.example/1.png",
        "https://b.example/2.png",
        "https://c.example/3.png",
    } <= found


def test_still_finds_tag_references() -> None:
    body = (
        b'<html><body><img src="/logo.png"><link rel="stylesheet" href="/style.css"></body></html>'
    )
    found = _referenced_assets(body, "https://blog.example.com/")
    assert "https://blog.example.com/logo.png" in found
    assert "https://blog.example.com/style.css" in found


def test_ignores_inline_and_non_fetchable_schemes() -> None:
    body = (
        b"<html><style>a{background:url(data:image/png;base64,AAAA)}</style>"
        b'<img src="data:image/gif;base64,R0lGOD"><a href="#top">x</a></html>'
    )
    assert _referenced_assets(body, "https://blog.example.com/") == set()


# ── HTML entities in attributes ──────────────────────────────────────────


def test_decodes_entities_in_attribute_urls() -> None:
    """`&amp;` in an attribute is one ampersand.

    Taken from a live Blogger capture, where the audit reported
    `…targetBlogID=548…&amp;zx=…` — a URL that is wrong on screen and can
    never match the captured `…&zx=…`, so a fetched asset gets listed as
    missing.
    """
    body = (
        b'<html><head><link rel="stylesheet" '
        b'href="https://www.blogger.com/dyn-css/authorization.css'
        b'?targetBlogID=5481807035341764022&amp;zx=a1f39d26-780f"></head></html>'
    )
    found = _referenced_assets(body, "https://blog.example.com/")
    assert (
        "https://www.blogger.com/dyn-css/authorization.css"
        "?targetBlogID=5481807035341764022&zx=a1f39d26-780f" in found
    )
    assert not any("&amp;" in u for u in found)


def test_a_captured_asset_with_an_entity_reference_is_not_reported_missing() -> None:
    """The false positive the bug produced, stated as the property that matters."""
    body = b'<html><img src="/img.png?a=1&amp;b=2"></html>'
    referenced = _referenced_assets(body, "https://blog.example.com/")
    captured = {"https://blog.example.com/img.png?a=1&b=2"}
    assert not (referenced - captured), f"false positive: {referenced - captured}"


def test_style_block_contents_are_not_entity_decoded() -> None:
    """<style> is raw text: `&amp;` there really is three characters.

    Decoding it would invent a URL the page never referenced.
    """
    body = b"<html><style>a{background:url(/x.png?a=1&amp;b=2)}</style></html>"
    found = _referenced_assets(body, "https://blog.example.com/")
    assert "https://blog.example.com/x.png?a=1&amp;b=2" in found


def test_inline_style_attributes_are_entity_decoded() -> None:
    body = b'<html><div style="background:url(/y.png?a=1&amp;b=2)"></div></html>'
    found = _referenced_assets(body, "https://blog.example.com/")
    assert "https://blog.example.com/y.png?a=1&b=2" in found


# ── naming the real target ───────────────────────────────────────────────


def test_recovers_the_intended_host_from_a_mangled_request() -> None:
    """So the warning can say which host the asset was actually on."""
    assert _escaped_target_host(BLOGGER_MANGLED) == "themes.googleusercontent.com"


def test_intended_host_of_an_ordinary_url_is_its_own_host() -> None:
    host = _escaped_target_host("https://blog.example.com/logo.png")
    assert host in ("blog.example.com", None)
