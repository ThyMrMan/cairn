"""The asset audit: what a capture referenced but does not contain.

Its whole job is to make a silent gap loud, so the cases that matter are the
ones nothing else reports — CSS-escaped URLs wget mishandles, and lazy-loaded
images it cannot see at all.
"""

from __future__ import annotations

import io
from pathlib import Path

from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, Site
from cairn.db.types import utcnow
from cairn.services import sites, storage
from cairn.services.htmlrefs import parse_page
from cairn.services.postprocess import (
    Context,
    _escaped_target_host,
    _is_success,
    _referenced_assets,
    _unescape_css,
    run_chain,
    step_asset_audit,
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


def test_finds_external_script_and_stylesheet_sources() -> None:
    """Stripping a <script> element must not take its own src with it.

    Removing the whole element rather than just its contents drops every
    external script from the results — which showed up as an analytics host
    being absent from the domain picker entirely, and would equally have hidden
    a missing script from the gap report.
    """
    body = (
        b'<html><head><script src="https://cdn.example/app.js">var inline = 1;</script>'
        b'<script>document.write("https://not-a-real-reference.example/x.js")</script>'
        b"<style>body{background:url(/bg.png)}</style></head></html>"
    )
    found = _referenced_assets(body, "https://blog.example.com/")
    assert "https://cdn.example/app.js" in found
    assert "https://blog.example.com/bg.png" in found
    # URLs written inside script bodies are not references; guessing at strings
    # in JavaScript is how bogus requests get made.
    assert not any("not-a-real-reference" in u for u in found)


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


# ── which records count as pages ─────────────────────────────────────────


def test_only_successful_responses_count_as_pages() -> None:
    assert _is_success("200")
    assert _is_success("204")
    assert not _is_success("404")
    assert not _is_success("301")
    assert not _is_success(None)


def _write_warc(path: Path, records: list[tuple[str, int, bytes]]) -> None:
    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    with open(path, "wb") as fh:
        writer = WARCWriter(fh, gzip=True)
        for url, status, body in records:
            headers = StatusAndHeaders(
                f"{status} OK", [("Content-Type", "text/html")], protocol="HTTP/1.1"
            )
            writer.write_record(
                writer.create_warc_record(
                    url, "response", payload=io.BytesIO(body), http_headers=headers
                )
            )


def test_a_404_page_does_not_contribute_its_own_assets_or_page_count(
    db: Session, settings: Settings, tmp_path: Path
) -> None:
    """`--content-on-error` archives the body of every 404, and a site's error
    template references its own logo. Counting those made the gap report claim
    assets were missing from a page nobody requested — and turned four real
    pages plus twelve mangled requests into "16 page(s)"."""
    warc_dir = tmp_path / storage.WARC_DIR
    warc_dir.mkdir(parents=True)
    _write_warc(
        warc_dir / "part.warc.gz",
        [
            ("https://blog.example.com/", 200, b'<html><img src="/real.png" data-src="/lazy.png">'),
            ("https://blog.example.com/gone", 404, b'<html><img src="/error-template.png">'),
        ],
    )

    ctx = Context(
        session=db,
        settings=settings,
        capture=Capture(),
        site=Site(),
        output_dir=tmp_path,
        tool_version=None,
        stats={},
        scope={},
        seeds=[],
        seed_source={},
        artifacts=[],
        warnings=[],
    )
    step_asset_audit(ctx)

    missing = " ".join(ctx.warnings)
    assert "real.png" in missing
    assert "error-template.png" not in missing
    # One page scanned, not two: the 404 body is not a page of this site.
    assert "in 1 page(s)" in missing


# ── warnings known before the crawl starts ───────────────────────────────


def test_a_never_indexed_site_is_reported_as_such(db: Session, settings: Settings) -> None:
    """A site with no saved scope rules falls back to seed-host-only. That is
    the right default and the wrong thing to do silently: the capture comes
    back with the HTML and none of the images, and nothing says why."""
    site = Site(
        slug="blog",
        title="Blog",
        seed_url="https://blog.example.com/",
        primary_host="blog.example.com",
        folder_id=1,
        archive_path="Unfiled/blog",
    )
    db.add(site)
    db.flush()

    assert sites.scope_is_unindexed(db, site)


def test_pre_crawl_warnings_reach_the_gap_report(
    db: Session, settings: Settings, tmp_path: Path
) -> None:
    """They are published to the live log as the crawl begins, but the log
    scrolls away and the capture outlives it."""
    ctx = run_chain(
        db,
        settings,
        capture=Capture(status="ok", warc_files=[], started_at=utcnow()),
        site=Site(seed_url="https://blog.example.com/", archive_path="Unfiled/blog"),
        output_dir=tmp_path,
        tool_version=None,
        stats={},
        scope={},
        seeds=["https://blog.example.com/"],
        warnings=["This site has never been indexed"],
    )

    assert any("never been indexed" in w for w in ctx.warnings)


# ── assets only a CSS escape reveals ─────────────────────────────────────


def test_a_css_escaped_reference_is_flagged_as_well_as_recorded() -> None:
    """Recording it is what makes the audit honest; flagging it is what makes
    the capture complete. wget requests the escaped text against the page's
    own host, 404s, and never learns the real URL exists — so unless these are
    handed over as seeds the asset is lost whatever the scope says."""
    body = (
        rb"<html><head><style>"
        rb"#h{background:url(https\:\/\/themes.googleusercontent.com\/image?id=abc&options=w480)}"
        rb"</style></head><body><img src='/plain.png'></body></html>"
    )
    page = parse_page(body, "https://blog.example.com/2026/08/post.html")
    skin = "https://themes.googleusercontent.com/image?id=abc&options=w480"

    assert skin in page.assets
    assert page.escaped_assets == {skin}
    # An ordinary reference is not flagged; it needs no help.
    assert "https://blog.example.com/plain.png" in page.assets


def test_an_unescaped_reference_to_the_same_host_is_not_flagged() -> None:
    body = (
        b"<html><head><style>"
        b"#h{background:url(https://themes.googleusercontent.com/image?id=abc)}"
        b"</style></head></html>"
    )
    page = parse_page(body, "https://blog.example.com/")
    assert "https://themes.googleusercontent.com/image?id=abc" in page.assets
    assert page.escaped_assets == set()
