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


def looks_blocked(body: bytes, url: str = "") -> Verdict:
    """Whether this response is a bypass page rather than content."""
    by_url = url_looks_blocked(url)
    if by_url.blocked:
        return Verdict(True, by_url.reason.replace("the URL is", "the final URL is"))

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
