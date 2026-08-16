"""Telling an interstitial apart from the page you wanted.

Three things need this and they must agree, or the tool contradicts itself:
the mint deciding whether a userscript worked, the Test button deciding
whether a jar is still good, and the post-capture scan deciding whether a
six-hour crawl archived four thousand copies of a content warning.

**It is a heuristic and it is treated as one.** A blog post *about* content
warnings contains the same words as a content warning. So the default check
needs corroboration — a marker phrase plus a page short enough to be an
interstitial rather than an article — and any profile that cares can pin the
answer exactly with a selector instead. Explicit beats clever: `success_
selector` and `interstitial_selector` are always believed over any of this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Phrases that appear on a bypass page and essentially nowhere else. Blogger's
# is the one this tool exists for, but the shape is common enough to be worth
# generalising slightly.
MARKERS = (
    re.compile(rb"I understand and wish to continue", re.IGNORECASE),
    re.compile(rb"content\s+warning", re.IGNORECASE),
    re.compile(rb"blog\s+contains\s+content\s+.{0,40}adult", re.IGNORECASE),
    re.compile(rb"viewer\s+discretion", re.IGNORECASE),
    re.compile(rb"you\s+must\s+be\s+(?:18|21|of\s+legal\s+age)", re.IGNORECASE),
)

# Blogger sends the interstitial from a distinct path, which is far stronger
# evidence than any phrase because no article is served from it.
#
# `/interstitial/` is the one Blogger actually uses today —
# `www.blogger.com/interstitial/blog?u=<blog>` — and its absence here is why a
# gated blog could be captured, marked partial, and say nothing about why.
URL_MARKERS = (
    "/interstitial/",
    "/content-warning",
    "blogger.com/blogin.g",
    "/b/blogger-warning",
)

# An interstitial is a short page with a button. A long one is an article that
# happens to use the words — the single most useful discriminator here, and
# the reason the phrase list can afford to be broad.
MAX_INTERSTITIAL_BYTES = 24_000

# A gate that arrives *inside* a good page instead of in place of one, which
# every check above is blind to.
#
# Blogger serves the real post — 200, full text, every asset — and injects a
# full-viewport iframe over it plus a rule hiding everything else:
#
#     <body class='loading'><iframe id="injected-iframe"
#        src="https://www.blogger.com/interstitial/blog?u=…"
#        style="…z-index:999; visibility:visible"></iframe>
#     <style>body { _height: 100%; } body * { visibility: hidden; }</style>
#
# Nothing above sees it. The URL is the blog's own, so `url_looks_blocked`
# says nothing, and the body is a 70-100 KB article, so `MAX_INTERSTITIAL_
# BYTES` returns CLEAR before a single phrase is tried. Measured on a real
# capture: 442 of 442 archived posts carried it, the pages were complete, and
# the capture was reported ready — four rounds of reading finished captures
# went by before anyone looked at the archived bytes.
#
# **Both halves are required, and both are structural rather than lexical.**
# An article *about* content warnings uses the words; it does not frame
# Blogger's gate and it does not hide its own body. Demanding the pair is what
# lets this run at any length, which is the entire point of having it.
_MARKER_ALTERNATION = b"|".join(re.escape(marker.encode("ascii")) for marker in URL_MARKERS)
OVERLAY_FRAME = re.compile(
    rb"<iframe[^>]{0,400}src=[\"'][^\"']{0,600}(?:" + _MARKER_ALTERNATION + rb")",
    re.IGNORECASE,
)
OVERLAY_HIDES_BODY = re.compile(rb"body\s*\*\s*\{[^}]{0,200}visibility\s*:\s*hidden", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Verdict:
    blocked: bool
    reason: str

    @property
    def ok(self) -> bool:
        return not self.blocked


CLEAR = Verdict(blocked=False, reason="")


def url_looks_blocked(url: str) -> Verdict:
    """Whether a URL alone is enough to call it an interstitial.

    Separate from `looks_blocked` because a **redirect** has no body to
    inspect, and a redirect is how a gated blog most often refuses: the seed
    answers 302 and points at a host the crawl is not allowed to follow. Left
    to the body check, that capture archives one record and explains nothing.
    """
    lowered = (url or "").lower()
    for marker in URL_MARKERS:
        if marker in lowered:
            return Verdict(True, f"the URL is an interstitial path ({marker})")
    return CLEAR


def overlay_blocked(body: bytes) -> Verdict:
    """Whether a *complete* page has a gate drawn on top of it.

    Kept separate from `looks_blocked` because the two need opposite advice.
    A classic interstitial means the content never arrived and the profile is
    the thing to fix. This means the content did arrive, in full, and the
    site drew over it because some per-browser flag — Blogger's
    `interstitialAccepted` — is still false. Telling somebody to re-mint
    cookies that demonstrably worked sends them in the wrong direction.
    """
    if not OVERLAY_FRAME.search(body):
        return CLEAR
    if not OVERLAY_HIDES_BODY.search(body):
        # A framed gate with the page still visible underneath is a banner,
        # not a gate. Only the pair hides the content.
        return CLEAR
    return Verdict(True, "an interstitial iframe is drawn over a hidden body")


def looks_blocked(body: bytes, url: str = "") -> Verdict:
    """Whether this response is a bypass page rather than content."""
    by_url = url_looks_blocked(url)
    if by_url.blocked:
        return Verdict(True, by_url.reason.replace("the URL is", "the final URL is"))

    # Before the length guard, deliberately: this shape arrives *as* a
    # full-length page, so anything checked after the guard cannot see it.
    overlay = overlay_blocked(body)
    if overlay.blocked:
        return overlay

    if len(body) > MAX_INTERSTITIAL_BYTES:
        # Long enough to be a real page. Say nothing rather than guess.
        return CLEAR

    for pattern in MARKERS:
        found = pattern.search(body)
        if found:
            phrase = found.group(0).decode("utf-8", "replace")[:60]
            return Verdict(True, f"the page is short and says {phrase!r}")
    return CLEAR


def judge(
    body: bytes,
    url: str,
    *,
    success_selector_found: bool | None = None,
    interstitial_selector_found: bool | None = None,
    body_must_not_match: str | None = None,
) -> Verdict:
    """The full decision, explicit signals first.

    Order matters: a profile that names a selector has told us exactly how to
    tell the two apart on *its* site, and the built-in guesswork must not
    override it. Guessing is only for profiles that said nothing.
    """
    if success_selector_found is not None:
        return (
            CLEAR
            if success_selector_found
            else Verdict(True, "the success selector was not on the page")
        )
    if interstitial_selector_found is not None:
        return (
            Verdict(True, "the interstitial selector is still on the page")
            if interstitial_selector_found
            else CLEAR
        )
    if body_must_not_match:
        try:
            if re.search(body_must_not_match.encode("utf-8"), body, re.IGNORECASE):
                return Verdict(True, f"the body matched {body_must_not_match!r}")
        except re.error:
            pass  # a bad pattern falls through to the heuristic
        else:
            return CLEAR
    return looks_blocked(body, url)
