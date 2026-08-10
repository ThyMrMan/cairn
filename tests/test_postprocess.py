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


# ── naming the real target ───────────────────────────────────────────────


def test_recovers_the_intended_host_from_a_mangled_request() -> None:
    """So the warning can say which host the asset was actually on."""
    assert _escaped_target_host(BLOGGER_MANGLED) == "themes.googleusercontent.com"


def test_intended_host_of_an_ordinary_url_is_its_own_host() -> None:
    host = _escaped_target_host("https://blog.example.com/logo.png")
    assert host in ("blog.example.com", None)
