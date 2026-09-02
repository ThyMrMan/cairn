"""One blog, several hostnames — and asking rather than assuming.

From a real archive of 7,654 pages. Every page carried at least one link to
`emilystg.blogspot.co.uk`, the UK address of the blog it was already archiving
as `.com`: 54 distinct alias URLs across 67,246 occurrences, 52 of them with
their canonical twin already in the WARC. Replay 404s on all of them, the link
checker called the archive clean, and one page — `/p/blog-page.html`, linked
*only* through the alias — was never captured at all.

The probe is the other half. Blogger serves `http://` with a 200 on some blogs
and a 301 on others, so the same platform needs opposite decisions and the only
way to know which is to ask.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Discovery
from cairn.discovery import aliases
from cairn.discovery.fetch import Probe
from cairn.discovery.hosts import ROLE_ALIAS, HostStat, apply_defaults
from cairn.services import sites as site_service
from cairn.services.scope import (
    HostRule,
    Scope,
    build_reject_patterns,
    http_twin_targets,
)

BLOG = "emilystg.blogspot.com"
UK = "emilystg.blogspot.co.uk"


# ── naming ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "candidate",
    [UK, "emilystg.blogspot.de", "emilystg.blogspot.com.au", "emilystg.blogspot.co.nz"],
)
def test_a_country_domain_is_the_same_blog(candidate: str) -> None:
    """No TLD list: the structure is the evidence, so a suffix Google adds next
    year works without a release."""
    assert aliases.is_alias(candidate, BLOG)
    assert aliases.canonical_form(candidate) == BLOG


@pytest.mark.parametrize(
    "candidate",
    [
        # A different blog on the same platform is a different person's site,
        # which is exactly what the PSL private section exists to keep apart.
        "someoneelse.blogspot.com",
        "someoneelse.blogspot.co.uk",
        "emilystg.wordpress.com",
        "emilystg.com",
        "www.blogger.com",
    ],
)
def test_a_different_blog_is_not_an_alias(candidate: str) -> None:
    assert not aliases.is_alias(candidate, BLOG)


def test_a_host_is_not_its_own_alias() -> None:
    """Callers use this to decide whether to *add* something."""
    assert not aliases.is_alias(BLOG, BLOG)


def test_aliases_among_names_what_each_one_stands_for() -> None:
    found = aliases.aliases_among([UK, "someoneelse.blogspot.de", "cdn.example"], {BLOG})
    assert found == {UK: BLOG}


# ── what the probe decides ───────────────────────────────────────────────


def test_a_redirecting_alias_is_preselected() -> None:
    """Measured against the live blog: every alias URL 302s to its twin, path
    and scheme preserved. So it costs a handful of redirect records and makes
    67,000 archived links resolve."""
    stats = [
        HostStat(host=BLOG, registrable=BLOG, is_seed_host=True),
        HostStat(host=UK, registrable=UK),
    ]
    apply_defaults(stats, None, addresses={UK: {"alias_of": BLOG, "alias": "redirects"}})
    alias = stats[1]
    assert alias.role == ROLE_ALIAS
    assert alias.crawl_pages and alias.fetch_assets
    assert BLOG in alias.reason


def test_an_alias_that_serves_its_own_content_is_left_off() -> None:
    """Then the resemblance is all it has in common, and crawling it as a
    duplicate would double the archive."""
    stats = [
        HostStat(host=BLOG, registrable=BLOG, is_seed_host=True),
        HostStat(host=UK, registrable=UK),
    ]
    apply_defaults(stats, None, addresses={UK: {"alias_of": BLOG, "alias": "serves"}})
    assert stats[1].role == ROLE_ALIAS
    assert not stats[1].crawl_pages
    assert "separate site" in stats[1].reason


def test_an_unreachable_alias_is_left_off() -> None:
    stats = [HostStat(host=UK, registrable=UK)]
    apply_defaults(stats, None, addresses={UK: {"alias_of": BLOG, "alias": "unreachable"}})
    assert not stats[0].crawl_pages


def test_hosts_nothing_probed_keep_the_defaults_they_had() -> None:
    stats = [HostStat(host="cdn.example", registrable="example", asset_refs=5)]
    apply_defaults(stats, None, addresses={})
    assert stats[0].fetch_assets
    assert stats[0].role != ROLE_ALIAS


# ── reading a probe's answer ─────────────────────────────────────────────


def test_a_probe_reports_the_three_outcomes_apart() -> None:
    serves = Probe(url="http://x/", status=200)
    moved = Probe(url="http://x/", status=301, location="https://x/")
    dead = Probe(url="http://x/", status=0, error="connection refused")
    assert serves.serves and not serves.redirects
    assert moved.redirects and not moved.serves
    assert not (dead.serves or dead.redirects)
    assert moved.redirects_to_host("x")
    assert not moved.redirects_to_host("elsewhere")


# ── the reject that follows from it ──────────────────────────────────────


def blog_scope(**kwargs: object) -> Scope:
    return Scope(
        seeds=[f"https://{BLOG}/"],
        hosts=[HostRule(BLOG, crawl_pages=True, fetch_assets=True)],
        **kwargs,  # type: ignore[arg-type]
    )


def test_a_site_that_redirects_http_is_not_given_the_reject() -> None:
    """The whole point of asking. Rejecting `http://` on a site that redirects
    costs the redirect chain, which can be the only route to a page — as it was
    for `/p/blog-page.html`, reachable in that archive by no other link."""
    scope = blog_scope(http_twin_hosts=[])
    assert http_twin_targets(scope) == []
    assert not any("^http://" in p for p in build_reject_patterns(scope))


def test_a_site_that_serves_http_content_still_gets_it() -> None:
    scope = blog_scope(http_twin_hosts=[BLOG])
    assert http_twin_targets(scope) == [BLOG]
    assert any("^http://" in p for p in build_reject_patterns(scope))


def test_nothing_probed_falls_back_to_the_conservative_guess() -> None:
    """None and [] must not mean the same thing: the first is "nobody asked",
    which keeps the guard, and the second is "asked, and no", which drops it.
    A crawl that never finishes is a worse mistake than a page reachable only
    through an http link."""
    assert http_twin_targets(blog_scope()) == [BLOG]
    assert http_twin_targets(blog_scope(http_twin_hosts=[])) == []


def test_a_probed_host_no_longer_crawled_is_ignored() -> None:
    scope = Scope(
        seeds=[f"https://{BLOG}/"],
        hosts=[HostRule(BLOG, crawl_pages=False, fetch_assets=True)],
        http_twin_hosts=[BLOG],
    )
    assert http_twin_targets(scope) == []


def test_the_answer_survives_the_job_spec() -> None:
    """The engine reads a serialized scope, and `or []` in `from_dict` would
    turn "asked, and no" back into "nobody asked" on the way through."""
    assert Scope.from_dict(blog_scope(http_twin_hosts=[]).to_dict()).http_twin_hosts == []
    assert Scope.from_dict(blog_scope().to_dict()).http_twin_hosts is None
    assert Scope.from_dict(blog_scope(http_twin_hosts=[BLOG]).to_dict()).http_twin_hosts == [BLOG]


# ── how the scope gets it ────────────────────────────────────────────────


def make_site(db: Session, settings: Settings, summary: dict | None = None):  # type: ignore[no-untyped-def]
    site = site_service.create_site(db, settings, seed_url=f"https://{BLOG}/", folder_id=1)
    site_service.save_scope(db, site, blog_scope())
    if summary is not None:
        db.add(Discovery(site_id=site.id, summary=summary))
    db.flush()
    return site


def test_a_site_with_no_discovery_keeps_the_guess(db: Session, settings: Settings) -> None:
    site = make_site(db, settings)
    assert site_service.resolved_scope(db, site).http_twin_hosts is None


def test_a_probed_site_uses_what_was_observed(db: Session, settings: Settings) -> None:
    site = make_site(db, settings, {"addresses": {BLOG: {"http": "redirects"}}})
    scope = site_service.resolved_scope(db, site)
    assert scope.http_twin_hosts == []
    assert not any("^http://" in p for p in build_reject_patterns(scope))


def test_a_site_observed_serving_http_gets_the_reject(db: Session, settings: Settings) -> None:
    site = make_site(db, settings, {"addresses": {BLOG: {"http": "serves"}}})
    scope = site_service.resolved_scope(db, site)
    assert scope.http_twin_hosts == [BLOG]
    assert any("^http://" in p for p in build_reject_patterns(scope))


def test_an_older_discovery_that_never_probed_keeps_the_guess(
    db: Session, settings: Settings
) -> None:
    """Every archive captured before this existed. It must keep the behaviour
    it has rather than quietly widening on an answer nobody gave."""
    site = make_site(db, settings, {"seed_host": BLOG})
    assert site_service.resolved_scope(db, site).http_twin_hosts is None


# ── the duplicate that was half-rejected ─────────────────────────────────


def test_both_mobile_markers_are_rejected() -> None:
    """`m=1` is the mobile duplicate and `m=0` is what Blogger appends when a
    visitor opts back out of it. Both serve the identical page the bare URL
    does, and only the first was rejected: measured on one crawl, 69,930 URLs
    carried `m=0` — 31% of the whole capture, every one a second copy of a
    page already being fetched.
    """
    import re as _re

    from cairn.discovery.platform import PRESETS

    pattern = _re.compile(next(p for p, _ in PRESETS["blogger"].reject_patterns if "m=" in p))
    assert pattern.search("https://b.blogspot.com/p.html?m=0")
    assert pattern.search("https://b.blogspot.com/p.html?m=1")
    assert pattern.search("https://b.blogspot.com/search?max-results=20&m=0")
    # And the page itself survives, or the crawl archives nothing.
    assert not pattern.search("https://b.blogspot.com/p.html")
    # `m=2` is not a thing Blogger emits; matching it would be guessing.
    assert not pattern.search("https://b.blogspot.com/p.html?m=2")


def test_the_narrower_pattern_is_retired_by_both_presets() -> None:
    """A correction only reaches scopes that already carry the old pattern if
    the preset names it as retired — otherwise the wrong rule stays in every
    scope that has it, indistinguishable from a deliberate choice."""
    from cairn.discovery.platform import PRESETS

    for preset_id in ("blogger", "blogger-lean"):
        assert r"[?&]m=1" in PRESETS[preset_id].retired_patterns, preset_id
