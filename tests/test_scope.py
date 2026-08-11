"""Scope resolution and the translation to wget flags.

The asset-host translation is M1's highest-risk piece, and these tests encode
what was established by running the real wget 1.25.0 from the container image
rather than what the manual implies. See test_capture_e2e.py for the paired
test that runs wget for real.
"""

from __future__ import annotations

import re

import pytest

from cairn.services.scope import (
    CSS_ESCAPE_REJECT,
    HostRule,
    Scope,
    ScopeError,
    asset_only_reject_pattern,
    combine_patterns,
    default_scope,
    to_wget_args,
)


def blog_scope(**kwargs: object) -> Scope:
    return Scope(
        seeds=["https://example.blogspot.com/"],
        hosts=[
            HostRule("example.blogspot.com", crawl_pages=True, fetch_assets=True),
            HostRule("1.bp.blogspot.com", crawl_pages=False, fetch_assets=True),
        ],
        **kwargs,  # type: ignore[arg-type]
    )


# ── defaults ─────────────────────────────────────────────────────────────


def test_default_scope_is_seed_host_only() -> None:
    """An unscoped default that spans hosts is how a crawl ends up on a
    neighbouring blog — the exact ArchiveBox failure this design removes."""
    scope = default_scope("https://example.blogspot.com/")
    assert [h.host for h in scope.hosts] == ["example.blogspot.com"]
    assert scope.hosts[0].crawl_pages
    assert scope.allowed_hosts == ["example.blogspot.com"]


@pytest.mark.parametrize("bad", ["", "not a url", "ftp://example.com/", "/relative"])
def test_default_scope_rejects_non_http_seeds(bad: str) -> None:
    with pytest.raises(ScopeError):
        default_scope(bad)


# ── validation ───────────────────────────────────────────────────────────


def test_scope_with_no_crawlable_host_is_rejected() -> None:
    """Assets-only everywhere would fetch nothing: there is no page to start
    from, so the crawl silently produces an empty archive."""
    scope = Scope(
        seeds=["https://example.com/"],
        hosts=[HostRule("example.com", crawl_pages=False, fetch_assets=True)],
    )
    with pytest.raises(ScopeError, match="no host is marked crawlable"):
        scope.validate()


def test_duplicate_host_rules_are_rejected() -> None:
    scope = Scope(
        seeds=["https://example.com/"],
        hosts=[
            HostRule("example.com", crawl_pages=True),
            HostRule("example.com", crawl_pages=False),
        ],
    )
    with pytest.raises(ScopeError, match="duplicate host"):
        scope.validate()


def test_host_both_included_and_excluded_is_rejected() -> None:
    scope = blog_scope(exclude_hosts=["1.bp.blogspot.com"])
    with pytest.raises(ScopeError, match="both included and excluded"):
        scope.validate()


def test_invalid_user_pattern_is_rejected_before_it_reaches_wget() -> None:
    scope = blog_scope(reject_patterns=["([unclosed"])
    with pytest.raises(ScopeError, match="invalid pattern"):
        scope.validate()


# ── the asset-host reject regex ──────────────────────────────────────────


def test_asset_host_regex_blocks_pages_and_allows_assets() -> None:
    rule = HostRule("1.bp.blogspot.com", crawl_pages=False, fetch_assets=True)
    pattern = re.compile(asset_only_reject_pattern(rule))

    # Rejected == not fetched.
    assert pattern.match("https://1.bp.blogspot.com/gallery.html")
    assert pattern.match("https://1.bp.blogspot.com/")
    # Assets survive, including with a query string.
    assert not pattern.match("https://1.bp.blogspot.com/photo.jpg")
    assert not pattern.match("https://1.bp.blogspot.com/style.css?v=3")
    assert not pattern.match("https://1.bp.blogspot.com/a/b/c/image.PNG".lower())
    # A different host is not this rule's business.
    assert not pattern.match("https://example.blogspot.com/gallery.html")


def test_asset_host_regex_tolerates_a_port() -> None:
    rule = HostRule("img.test", crawl_pages=False)
    pattern = re.compile(asset_only_reject_pattern(rule))
    assert pattern.match("http://img.test:8099/gallery.html")
    assert not pattern.match("http://img.test:8099/photo.jpg")


def test_extensionless_urls_are_dropped_by_default() -> None:
    """The cost of the safe default, pinned so it cannot change silently.

    Blogger proxies images through extension-less URLs; those are lost unless
    the host opts in. No regex over URLs can tell such an image from a page,
    so this is a deliberate trade, not an oversight.
    """
    rule = HostRule("lh3.googleusercontent.com", crawl_pages=False)
    pattern = re.compile(asset_only_reject_pattern(rule))
    assert pattern.match("https://lh3.googleusercontent.com/blogger_img_proxy/AEn0k_abc")


def test_allow_extensionless_admits_proxy_images_but_still_blocks_pages() -> None:
    rule = HostRule("lh3.googleusercontent.com", crawl_pages=False, allow_extensionless=True)
    pattern = re.compile(asset_only_reject_pattern(rule))
    assert not pattern.match("https://lh3.googleusercontent.com/blogger_img_proxy/AEn0k_abc")
    assert not pattern.match("https://lh3.googleusercontent.com/photo.jpg")
    assert pattern.match("https://lh3.googleusercontent.com/gallery.html")


def test_host_metacharacters_cannot_alter_the_pattern() -> None:
    """Hosts come from discovery and user input and end up inside a regex that
    ends up in a subprocess argument."""
    rule = HostRule("evil.com|other.com", crawl_pages=False)
    pattern = re.compile(asset_only_reject_pattern(rule))
    assert not pattern.match("https://other.com/anything.html")


def test_combined_patterns_cannot_leak_across_branches() -> None:
    """A user pattern with a top-level alternation must not swallow the next."""
    combined = re.compile(combine_patterns(["^https://a/", "b$"]))
    assert combined.search("https://a/x")
    assert combined.search("zzb")
    assert not combined.search("https://c/x")


# ── wget translation ─────────────────────────────────────────────────────


def test_translation_lists_every_allowed_host_in_domains() -> None:
    """--domains is a hard gate: --page-requisites does NOT bypass it, so an
    asset host omitted here loses its images entirely (verified against
    wget 1.25.0)."""
    args = to_wget_args(blog_scope()).args
    domains = next(a for a in args if a.startswith("--domains="))
    assert "example.blogspot.com" in domains
    assert "1.bp.blogspot.com" in domains
    assert "--span-hosts" in args


def test_translation_requires_pcre_when_an_asset_host_exists() -> None:
    args = to_wget_args(blog_scope()).args
    assert "--regex-type=pcre" in args
    assert any(a.startswith("--reject-regex=") for a in args)


def test_posix_is_refused_rather_than_silently_producing_a_broken_pattern() -> None:
    """POSIX ERE has no lookahead. Emitting the pattern anyway gives wget an
    'Invalid preceding regular expression' at crawl time, hours later."""
    with pytest.raises(ScopeError, match="POSIX"):
        to_wget_args(blog_scope(), regex_type="posix")


def test_the_only_reject_for_a_single_crawlable_host_is_the_css_escape_guard() -> None:
    """No asset host means no per-host fencing, but the CSS-escape reject is
    unconditional: any scope can meet a skin that writes `url(https\\:\\/\\/…)`,
    and wget requests that shape against the site itself, once per variant."""
    scope = Scope(
        seeds=["https://example.com/"],
        hosts=[HostRule("example.com", crawl_pages=True, fetch_assets=True)],
    )
    reject = next(a for a in to_wget_args(scope).args if a.startswith("--reject-regex="))
    assert reject == f"--reject-regex=(?:{CSS_ESCAPE_REJECT})"


def test_the_css_escape_reject_matches_what_wget_actually_requests() -> None:
    """The literal URL from a real Blogger capture: twelve of these 404'd."""
    mangled = (
        "https://jsrandomtest29.blogspot.com/2026/08/https%5C:%5C/%5C/"
        "themes.googleusercontent.com%5C/image?id=L1lcAxxz&options=w640"
    )
    intended = "https://themes.googleusercontent.com/image?id=L1lcAxxz&options=w640"
    assert re.search(CSS_ESCAPE_REJECT, mangled)
    assert not re.search(CSS_ESCAPE_REJECT, intended)


def test_user_reject_patterns_are_merged_with_generated_ones() -> None:
    scope = blog_scope(reject_patterns=[r"[?&]m=1"])
    reject = next(a for a in to_wget_args(scope).args if a.startswith("--reject-regex="))
    assert "m=1" in reject
    assert "1\\.bp\\.blogspot\\.com" in reject


def test_limits_and_robots_translate() -> None:
    scope = blog_scope(max_bytes=1024, obey_robots=False, max_depth=3)
    args = to_wget_args(scope).args
    assert "--quota=1024" in args
    assert "--level=3" in args
    assert "-e" in args and "robots=off" in args


def test_unlimited_depth_is_the_default() -> None:
    """ArchiveBox's depth ceiling is the failure this avoids; nothing should
    quietly reintroduce one."""
    assert "--level=inf" in to_wget_args(blog_scope()).args


def test_notes_warn_about_dropped_extensionless_assets() -> None:
    notes = to_wget_args(blog_scope()).notes
    assert any("extension" in n for n in notes)


def test_max_pages_is_reported_as_supervisor_enforced() -> None:
    """wget has no page cap; claiming otherwise in the UI would be a lie."""
    notes = to_wget_args(blog_scope(max_pages=100)).notes
    assert any("supervisor" in n for n in notes)


def test_roundtrip_through_dict_preserves_everything() -> None:
    scope = blog_scope(reject_patterns=[r"[?&]m=1"], max_bytes=42, obey_robots=False)
    scope.hosts[1].allow_extensionless = True
    restored = Scope.from_dict(scope.to_dict())
    assert restored.to_dict() == scope.to_dict()
    assert restored.hosts[1].allow_extensionless
