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
from cairn.db.models import Capture, EngineRecord, Site
from cairn.db.types import utcnow
from cairn.services import interstitial, sites, storage
from cairn.services.htmlrefs import parse_page
from cairn.services.postprocess import (
    Context,
    _escaped_target_host,
    _is_success,
    _partition_missing,
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


def _lazy_audit_ctx(db: Session, settings: Settings, tmp_path: Path, engine_id: str) -> Context:
    warc_dir = tmp_path / storage.WARC_DIR
    warc_dir.mkdir(parents=True, exist_ok=True)
    _write_warc(
        warc_dir / "part.warc.gz",
        [("https://blog.example.com/", 200, b'<html><img src="/real.png" data-src="/lazy.png">')],
    )
    return Context(
        session=db,
        settings=settings,
        capture=Capture(engine_id=engine_id),
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


def test_the_lazy_image_warning_names_the_engine_that_ran(
    db: Session, settings: Settings, tmp_path: Path
) -> None:
    """Reported from a real manifest: a browsertrix capture whose warnings said
    "wget cannot execute JavaScript, so those images are not in this archive".

    Both halves were wrong. It named an engine that had not run, and it told
    somebody who had just used a browser engine to go and use a browser engine.
    The capability is read from the engine's own manifest, so a third engine
    gets the right sentence without this code learning its name.
    """
    # The real record, synced from the shipped manifest — so this asserts
    # against what browsertrix actually declares rather than a fixture that
    # could drift from it.
    assert db.get(EngineRecord, "browsertrix").manifest["capabilities"]["javascript"] is True

    ctx = _lazy_audit_ctx(db, settings, tmp_path, "browsertrix")
    step_asset_audit(ctx)
    lazy = next(w for w in ctx.warnings if "lazy-loaded" in w)

    assert "wget" not in lazy
    assert "autofetch" in lazy


def test_a_non_scripting_engine_still_says_the_images_are_absent(
    db: Session, settings: Settings, tmp_path: Path
) -> None:
    """The half that was right, and must stay right."""
    assert db.get(EngineRecord, "wget-warc").manifest["capabilities"]["javascript"] is False

    ctx = _lazy_audit_ctx(db, settings, tmp_path, "wget-warc")
    step_asset_audit(ctx)
    lazy = next(w for w in ctx.warnings if "lazy-loaded" in w)

    assert "wget-warc does not execute JavaScript" in lazy
    assert "not in this archive" in lazy


def test_an_unknown_engine_warns_rather_than_reassures(
    db: Session, settings: Settings, tmp_path: Path
) -> None:
    """A capture whose engine has since been removed still has to say something.

    It says the images may be missing. The other way round would tell somebody
    their archive is complete on the strength of a record that is not there.
    """
    ctx = _lazy_audit_ctx(db, settings, tmp_path, "since-uninstalled")
    step_asset_audit(ctx)
    lazy = next(w for w in ctx.warnings if "lazy-loaded" in w)

    assert "since-uninstalled does not execute JavaScript" in lazy


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


# ── in scope and absent, versus deliberately out of scope ────────────────

BLOG_SCOPE = {
    "hosts": [
        {"host": "blog.example.com", "crawl_pages": True, "fetch_assets": True},
        {"host": "cdn.example.net", "crawl_pages": False, "fetch_assets": True},
    ],
    "reject_patterns": [r"[?&]m=1"],
    "seeds": ["https://blog.example.com/"],
}


def _partition(missing: list[str]) -> tuple[list[str], list[str]]:
    ctx = Context(
        session=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
        capture=Capture(),
        site=Site(),
        output_dir=Path("."),
        tool_version=None,
        stats={},
        scope=BLOG_SCOPE,
        seeds=[],
        seed_source={},
        artifacts=[],
        warnings=[],
    )
    return _partition_missing(ctx, missing)


def test_an_unticked_host_is_a_setting_not_a_gap() -> None:
    """www.blogger.com is off by preset — its admin CSS and comment iframe
    have no archival value. Counting that as a missing asset means every
    Blogger capture forever opens with a warning about working as intended,
    which is how a real gap stops being noticed."""
    absent, excluded = _partition(
        [
            "https://www.blogger.com/dyn-css/authorization.css?targetBlogID=548",
            "https://blog.example.com/logo.png",
        ]
    )
    assert absent == ["https://blog.example.com/logo.png"]
    assert excluded == ["https://www.blogger.com/dyn-css/authorization.css?targetBlogID=548"]


def test_a_reject_pattern_is_as_deliberate_as_an_unticked_box() -> None:
    absent, excluded = _partition(["https://blog.example.com/post.html?m=1"])
    assert absent == []
    assert len(excluded) == 1


def test_an_in_scope_asset_host_still_reports_real_misses() -> None:
    absent, excluded = _partition(["https://cdn.example.net/img/hero.jpg"])
    assert absent == ["https://cdn.example.net/img/hero.jpg"]
    assert excluded == []


def test_an_unparseable_scope_reports_everything_as_absent() -> None:
    """The cautious direction: never explain a gap away on a guess."""
    ctx = Context(
        session=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
        capture=Capture(),
        site=Site(),
        output_dir=Path("."),
        tool_version=None,
        stats={},
        scope={"hosts": [{"crawl_pages": True}]},  # a host rule with no host
        seeds=[],
        seed_source={},
        artifacts=[],
        warnings=[],
    )
    absent, excluded = _partition_missing(ctx, ["https://anywhere.example/x.png"])
    assert absent == ["https://anywhere.example/x.png"]
    assert excluded == []


def test_a_scope_with_no_asset_hosts_reports_everything_as_absent() -> None:
    """Same reason as an unparseable scope: calling a gap deliberate needs
    positive evidence that somebody chose it."""
    ctx = Context(
        session=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
        capture=Capture(),
        site=Site(),
        output_dir=Path("."),
        tool_version=None,
        stats={},
        scope={},
        seeds=[],
        seed_source={},
        artifacts=[],
        warnings=[],
    )
    absent, excluded = _partition_missing(ctx, ["https://blog.example.com/logo.png"])
    assert absent == ["https://blog.example.com/logo.png"]
    assert excluded == []


# ── turned away at the door ──────────────────────────────────────────────
#
# Reported from a real run against a gated Blogger blog. The seed answered 302
# to www.blogger.com/interstitial/blog?u=…, which is out of scope, so the
# capture archived one redirect — and said nothing at all about why, because
# interstitial detection only ever inspected the body of a 200 and a redirect
# has no body. What the person met instead was pywb, in an iframe, reporting a
# URL they had never entered as missing from the collection.


def test_bloggers_interstitial_path_is_recognised() -> None:
    """The path Blogger actually uses. Its absence is why this was silent."""
    verdict = interstitial.url_looks_blocked(
        "https://www.blogger.com/interstitial/blog?u=https://example.blogspot.com/"
    )
    assert verdict.blocked
    assert "/interstitial/" in verdict.reason


def test_an_ordinary_url_is_not_an_interstitial() -> None:
    assert not interstitial.url_looks_blocked(
        "https://example.blogspot.com/2026/08/post.html"
    ).blocked
    assert not interstitial.url_looks_blocked("").blocked


# ── let in, then curtained off ───────────────────────────────────────────
#
# The other half of the same story, and the expensive half. Blogger answered
# 200 with the whole post — text, images, every asset — and injected an iframe
# over it plus `body * { visibility: hidden }`. Measured on a real capture:
# every one of 442 archived posts carried it, the capture was reported ready,
# and four rounds of reading finished captures went by before anyone read the
# archived bytes. Both existing checks are blind to it by construction: the
# URL is the blog's own, and at 70-100 KB the body is far past the length
# guard that makes the phrase list safe.

OVERLAY_PAGE = (
    b"<html><head><title>Kind of a Stretch</title></head>"
    b"<body class='loading'><iframe id=\"injected-iframe\" "
    b'src="https://www.blogger.com/interstitial/blog?u=https://example.blogspot.com/p.html" '
    b'style="position:absolute; z-index:999; visibility:visible"></iframe>'
    b"<style>body { _height: 100%; } body * { visibility: hidden; }</style>"
    b"<div class='post-body'>" + b"the real post, archived in full. " * 3000 + b"</div>"
    b"</body></html>"
)


def test_a_content_warning_drawn_over_a_whole_page_is_caught() -> None:
    """The shape both other checks are built to miss."""
    assert len(OVERLAY_PAGE) > interstitial.MAX_INTERSTITIAL_BYTES
    assert interstitial.overlay_blocked(OVERLAY_PAGE).blocked
    # And it reaches the shared entry point, which is what the profile test,
    # the mint and the jar check all call. Each of those reported "real
    # content" on exactly this page.
    assert interstitial.looks_blocked(OVERLAY_PAGE, "https://example.blogspot.com/p.html").blocked


def test_an_article_about_content_warnings_is_not_an_overlay() -> None:
    """The false positive the pair of conditions exists to prevent.

    A post that uses every phrase in `MARKERS` but frames nothing and hides
    nothing is a post. This is why the check is structural.
    """
    article = (
        b"<html><body><div class='post-body'>"
        b"I understand and wish to continue is what the content warning says. "
        b"You must be 18. Viewer discretion advised. " * 400 + b"</div></body></html>"
    )
    assert not interstitial.overlay_blocked(article).blocked
    assert not interstitial.looks_blocked(article, "https://example.blogspot.com/p.html").blocked


def test_a_framed_gate_over_a_visible_page_is_only_a_banner() -> None:
    """Half the signature is not the signature. Without the hiding rule the
    page is readable underneath, and calling that blocked would flag any site
    that merely embeds something."""
    banner = OVERLAY_PAGE.replace(b"body * { visibility: hidden; }", b"")
    assert not interstitial.overlay_blocked(banner).blocked


def test_pages_hidden_under_an_overlay_are_reported_without_blaming_the_profile(
    db: Session, settings: Settings, tmp_path: Path
) -> None:
    """The advice is the point. The old wording — re-mint the cookies — is
    exactly wrong here, because the cookies worked: that is how a complete
    page arrived to be drawn over."""
    warc_dir = tmp_path / storage.WARC_DIR
    warc_dir.mkdir(parents=True, exist_ok=True)
    _write_warc(
        warc_dir / "part-00000.warc.gz",
        [("https://example.blogspot.com/p.html", 200, OVERLAY_PAGE)],
    )
    ctx = Context(
        session=db,
        settings=settings,
        capture=Capture(status="ok", warc_files=[], started_at=utcnow()),
        site=Site(seed_url="https://example.blogspot.com/", archive_path="Unfiled/blog"),
        output_dir=tmp_path,
        tool_version=None,
        stats={},
        scope={},
        seeds=["https://example.blogspot.com/"],
        seed_source={"manual": 1},
        artifacts=[],
        warnings=[],
    )
    settings.replay_uncover_overlays = False
    step_asset_audit(ctx)

    assert ctx.stats["overlay_pages"] == 1
    # Counted in its own bucket, so the two never inflate one another.
    assert ctx.stats["interstitial_pages"] == 0
    assert ctx.capture.status == "partial"
    message = " ".join(ctx.warnings)
    assert "archived in full" in message
    assert "not the problem" in message
    # Never blame the profile, and never promise the one fix that was measured
    # not to hold: the same acceptance cookie and user agent were honoured on
    # one run and refused ten hours later. Advice that sends somebody to spend
    # a multi-hour capture on a coin flip is worse than no advice.
    assert "re-mint" not in message.lower()
    assert "do not count on it" in message
    # It names something that works today instead of only something that might.
    assert "reader view" in message


def test_an_overlay_replay_will_uncover_is_not_a_partial_capture(
    db: Session, settings: Settings, tmp_path: Path
) -> None:
    """Reported: every page rendered and the capture still said Partial.

    The two changes landed in sequence and the second invalidated the first's
    premise. `partial` is not cosmetic — it fires the capture-incomplete
    notification and counts in the digest, so on a blog captured to a schedule
    this would have cried wolf once per run, forever. A warning that is always
    wrong is how the next real one gets ignored.
    """
    warc_dir = tmp_path / storage.WARC_DIR
    warc_dir.mkdir(parents=True, exist_ok=True)
    _write_warc(
        warc_dir / "part-00000.warc.gz",
        [("https://example.blogspot.com/p.html", 200, OVERLAY_PAGE)],
    )
    ctx = Context(
        session=db,
        settings=settings,
        capture=Capture(status="ok", warc_files=[], started_at=utcnow()),
        site=Site(seed_url="https://example.blogspot.com/", archive_path="Unfiled/blog"),
        output_dir=tmp_path,
        tool_version=None,
        stats={},
        scope={},
        seeds=["https://example.blogspot.com/"],
        seed_source={"manual": 1},
        artifacts=[],
        warnings=[],
    )
    assert settings.replay_uncover_overlays, "the default this test is about"
    step_asset_audit(ctx)

    assert ctx.capture.status == "ok"
    # Still counted and still declared. Silence would be the other error: the
    # rendering differs from the archived bytes, and that has to be sayable.
    assert ctx.stats["overlay_pages"] == 1
    message = " ".join(ctx.warnings)
    assert "Replay is showing the pages" in message
    assert "No action is needed" in message
    # Nothing that reads as a defect to fix.
    assert "do not count on it" not in message


def redirect_warc(path: Path, source: str, target: str) -> None:
    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        writer = WARCWriter(fh, gzip=True)
        headers = StatusAndHeaders(
            "302 Moved Temporarily", [("Location", target)], protocol="HTTP/1.1"
        )
        writer.write_record(writer.create_warc_record(source, "response", http_headers=headers))


def run_audit_on(db: Session, settings: Settings, tmp_path: Path, source: str, target: str):
    redirect_warc(tmp_path / storage.WARC_DIR / "part-00000.warc.gz", source, target)
    ctx = Context(
        session=db,
        settings=settings,
        capture=Capture(status="ok", warc_files=[], started_at=utcnow()),
        site=Site(seed_url=source, archive_path="Unfiled/blog"),
        output_dir=tmp_path,
        tool_version=None,
        stats={},
        scope={},
        seeds=[source],
        seed_source={"manual": 1},
        artifacts=[],
        warnings=[],
    )
    step_asset_audit(ctx)
    return ctx


def test_a_seed_redirected_to_a_content_warning_says_so(
    db: Session, settings: Settings, tmp_path: Path
) -> None:
    seed = "https://example.blogspot.com/"
    ctx = run_audit_on(
        db, settings, tmp_path, seed, f"https://www.blogger.com/interstitial/blog?u={seed}"
    )

    assert ctx.capture.status == "partial"
    assert ctx.stats["gate_redirects"] == 1
    message = " ".join(ctx.warnings)
    assert "content warning" in message
    assert "access profile" in message
    # It names both ends, so somebody can act on it without opening the WARC.
    assert seed in message
    assert "blogger.com/interstitial" in message


def test_a_redirect_off_scope_that_archived_nothing_is_explained(
    db: Session, settings: Settings, tmp_path: Path
) -> None:
    """Not every gate is recognisable. "The site sent us somewhere this capture
    may not go, and nothing else came back" is the explanation regardless."""
    ctx = run_audit_on(
        db, settings, tmp_path, "https://example.com/", "https://sso.example.net/login?next=/"
    )

    assert ctx.capture.status == "partial"
    assert ctx.stats["gate_redirects"] == 0
    assert ctx.stats["redirects"] == 1
    message = " ".join(ctx.warnings)
    assert "archived no pages" in message
    assert "sso.example.net" in message


def test_a_redirect_beside_real_pages_is_not_a_failure(
    db: Session, settings: Settings, tmp_path: Path
) -> None:
    """An ordinary site redirects constantly — http to https, a trailing
    slash, a moved post. Reporting every one of those would be noise."""
    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    warc = tmp_path / storage.WARC_DIR / "part-00000.warc.gz"
    warc.parent.mkdir(parents=True, exist_ok=True)
    with open(warc, "wb") as fh:
        writer = WARCWriter(fh, gzip=True)
        writer.write_record(
            writer.create_warc_record(
                "https://example.com/old",
                "response",
                http_headers=StatusAndHeaders(
                    "301 Moved Permanently",
                    [("Location", "https://example.com/new")],
                    protocol="HTTP/1.1",
                ),
            )
        )
        writer.write_record(
            writer.create_warc_record(
                "https://example.com/new",
                "response",
                payload=io.BytesIO(b"<html><body><p>a real page</p></body></html>"),
                http_headers=StatusAndHeaders(
                    "200 OK", [("Content-Type", "text/html")], protocol="HTTP/1.1"
                ),
            )
        )

    ctx = Context(
        session=db,
        settings=settings,
        capture=Capture(status="ok", warc_files=[], started_at=utcnow()),
        site=Site(seed_url="https://example.com/", archive_path="Unfiled/blog"),
        output_dir=tmp_path,
        tool_version=None,
        stats={},
        scope={},
        seeds=["https://example.com/"],
        seed_source={"manual": 1},
        artifacts=[],
        warnings=[],
    )
    step_asset_audit(ctx)

    assert ctx.capture.status == "ok"
    assert ctx.stats["redirects"] == 1
    assert not any("archived no pages" in w for w in ctx.warnings)
