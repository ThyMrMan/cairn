"""The userscript half of M5: metadata, the shim, and the mint itself.

The parsing tests run anywhere. The mint tests need Chromium, so they run in
the container and in CI and skip elsewhere — the same arrangement as the wget
and pywb suites.

Nothing here touches a real site or a real account: the fixture serves an
interstitial of exactly Blogger's shape and gates everything behind a cookie
its own button sets.
"""

from __future__ import annotations

import pytest

from cairn.services import browser, interstitial, userscripts
from tests.conftest import GATE_COOKIE

needs_browser = pytest.mark.skipif(
    not browser.availability()[0], reason="needs Playwright and Chromium"
)

DISMISSER = """
// ==UserScript==
// @name         Dismiss the content warning
// @version      1.0
// @match        http://127.0.0.1/*
// @grant        GM_setValue
// @run-at       document-start
// ==/UserScript==
GM_setValue('ran', true);
document.addEventListener('DOMContentLoaded', function () {
  var button = document.getElementById('continue');
  if (button) button.click();
});
"""


# ── metadata ─────────────────────────────────────────────────────────────


def test_the_metadata_block_is_read() -> None:
    script = userscripts.parse(DISMISSER)

    assert script.name == "Dismiss the content warning"
    assert script.version == "1.0"
    assert script.run_at == "document-start"
    assert script.matches == ["http://127.0.0.1/*"]
    assert script.grants == ["GM_setValue"]


def test_a_script_with_no_metadata_still_runs() -> None:
    """Tampermonkey accepts one, so refusing would be stricter than the thing
    people are porting from."""
    script = userscripts.parse("document.title = 'hi';")

    assert script.body
    assert any("metadata block" in w for w in script.warnings)


def test_grants_the_shim_cannot_honour_are_named() -> None:
    script = userscripts.parse(
        "// ==UserScript==\n// @grant GM_download\n// @grant GM_setValue\n// ==/UserScript==\n"
    )

    assert any("GM_download" in w for w in script.warnings)
    assert not any("GM_setValue" in w for w in script.warnings), (
        "a supported grant should not be warned about"
    )


def test_requires_are_reported_because_they_are_not_fetched() -> None:
    script = userscripts.parse(
        "// ==UserScript==\n// @require https://example.com/jquery.js\n// ==/UserScript==\n"
    )

    assert any("@require" in w for w in script.warnings)


@pytest.mark.parametrize(
    ("pattern", "url", "expected"),
    [
        ("http://127.0.0.1/*", "http://127.0.0.1:8080/post.html", True),
        ("*://*.blogspot.com/*", "https://example.blogspot.com/2019/post.html", True),
        ("*://*.blogspot.com/*", "https://blogspot.com/", True),
        ("*://*.blogspot.com/*", "https://notblogspot.com/", False),
        ("https://example.com/*", "http://example.com/", False),
        ("<all_urls>", "https://anything.example/", True),
        ("*://example.com/blog/*", "https://example.com/other/", False),
        # Chrome's patterns carry no port; Tampermonkey accepts one and people
        # write them. Reading `host:port` as a hostname makes every pattern
        # with a port match nothing, which is a silent "your script never ran".
        ("http://127.0.0.1:8080/*", "http://127.0.0.1:8080/post.html", True),
        ("http://127.0.0.1:8080/*", "http://127.0.0.1:9999/post.html", False),
        ("http://127.0.0.1/*", "http://127.0.0.1:8080/post.html", True),
    ],
)
def test_match_patterns_follow_chrome_rules(pattern: str, url: str, expected: bool) -> None:
    """Not glob and not regex. `*.` in a host means "or any subdomain", while
    `*` in a path means "any characters" — treating them as one thing gives a
    checker that passes everything."""
    script = userscripts.parse(f"// ==UserScript==\n// @match {pattern}\n// ==/UserScript==\n")

    assert userscripts.matches_url(script, url)[0] is expected


def test_a_script_that_would_never_have_run_says_so() -> None:
    script = userscripts.parse(
        "// ==UserScript==\n// @match https://other.example/*\n// ==/UserScript==\n"
    )

    ok, why = userscripts.matches_url(script, "https://example.blogspot.com/")

    assert not ok
    assert "would not have run in Tampermonkey either" in why


def test_the_shim_is_injected_before_the_script() -> None:
    """Order is the whole contract: the script's first line may call GM_setValue."""
    script = userscripts.parse(DISMISSER)
    injected = userscripts.init_script(script)

    assert injected.index("GM_setValue = function") < injected.index("GM_setValue('ran'")


# ── the interstitial heuristic ───────────────────────────────────────────


def test_a_short_page_with_the_phrase_is_an_interstitial() -> None:
    verdict = interstitial.looks_blocked(
        b"<html><body><h1>Content warning</h1><button>I understand and wish to continue</button>"
        b"</body></html>"
    )

    assert verdict.blocked


def test_a_long_article_using_the_same_words_is_not() -> None:
    """The discriminator that lets the phrase list be broad: an interstitial is
    a short page with a button, an essay about content warnings is not."""
    article = b"<html><body><h1>On content warnings</h1>" + (b"<p>essay</p>" * 4000) + b"</body>"

    assert interstitial.looks_blocked(article).ok


def test_an_explicit_selector_beats_the_guesswork() -> None:
    """A profile that names a selector has said exactly how to tell the two
    apart on its site; the built-in guess must not override it."""
    body = b"<html><body>Content warning</body></html>"

    assert interstitial.judge(body, "https://x.example/", success_selector_found=True).ok
    assert interstitial.judge(body, "https://x.example/", interstitial_selector_found=False).ok


# ── cookies out ──────────────────────────────────────────────────────────


def test_a_session_cookie_becomes_netscape_zero_not_minus_one() -> None:
    """Playwright says -1 for a session cookie and Netscape spells it 0. Pass
    -1 through and wget reads a cookie that expired in 1969 and drops it —
    usually the only cookie that mattered."""
    jar = browser.to_netscape(
        [{"name": "consent", "value": "1", "domain": ".example.com", "path": "/", "expires": -1}]
    )

    line = next(row for row in jar.splitlines() if row.startswith(".example.com"))
    assert line.split("\t")[4] == "0"
    assert line.split("\t")[1] == "TRUE", "a leading dot means include subdomains"


# ── the mint, against a real browser ─────────────────────────────────────


@needs_browser
async def test_a_userscript_mints_a_working_jar(gated_server: str) -> None:
    from cairn.services import mint as mint_service

    result = await mint_service.mint(
        script_text=DISMISSER.replace("http://127.0.0.1/*", f"{gated_server}*"),
        verify_url=gated_server,
    )

    assert result.ok, result.reason
    assert GATE_COOKIE in (result.cookies_text or "")
    assert result.cookie_count >= 1
    assert result.screenshot is not None


@needs_browser
async def test_a_script_that_does_nothing_reports_the_interstitial(gated_server: str) -> None:
    from cairn.services import mint as mint_service

    result = await mint_service.mint(script_text="// does nothing", verify_url=gated_server)

    assert not result.ok
    assert "interstitial" in result.reason


@needs_browser
async def test_a_mismatched_script_is_refused_before_the_browser_starts(
    gated_server: str,
) -> None:
    from cairn.services import mint as mint_service

    elsewhere = "// ==UserScript==\n// @match https://elsewhere.example/*\n// ==/UserScript==\n"
    result = await mint_service.mint(script_text=elsewhere, verify_url=gated_server)

    assert not result.ok
    assert "Tampermonkey" in result.reason
    assert result.final_url == "", "no browser should have been launched"
