"""Pulling references out of HTML, correctly.

Two callers need this and must agree: discovery counts which hosts a site
pulls from, and the post-capture asset audit checks what a page asked for
against what arrived. If they extracted differently, the picker would offer
hosts the audit then reported as missing, or the reverse.

Two decoding rules do most of the work here, and both were learned from real
Blogger pages rather than from the specs:

**HTML entities are decoded in attribute values, and not inside `<style>`.**
An attribute's `&amp;` is one ampersand; inside `<style>`, which is raw text,
it is three characters. Reporting the raw source text gives a URL that is
wrong on screen and that can never match what was actually fetched.

**CSS backslash escapes are decoded everywhere they appear.** Blogger skins
write theme images as `url(https\\:\\/\\/host\\/image?id=…)`. A browser
unescapes that; wget does not, which is why those URLs 404 against the blog
itself. Decoding is what lets both callers see the host that was really meant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from urllib.parse import urljoin, urlsplit

# Attributes that carry a subresource the page needs to render.
_ASSET_TAGS = re.compile(
    r"""<(?:img|script|source|video|audio|embed|track|input)\b[^>]*?"""
    r"""\b(?:src|data-src|poster)\s*=\s*["']([^"'>]+)["']""",
    re.IGNORECASE,
)
_SRCSET = re.compile(
    r"""<(?:img|source)\b[^>]*?\b(?:srcset|data-srcset)\s*=\s*["']([^"'>]+)["']""",
    re.IGNORECASE,
)
_OBJECT_DATA = re.compile(r"""<object\b[^>]*?\bdata\s*=\s*["']([^"'>]+)["']""", re.IGNORECASE)
_IFRAME = re.compile(r"""<iframe\b[^>]*?\bsrc\s*=\s*["']([^"'>]+)["']""", re.IGNORECASE)
_LINK_TAG = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_ANCHOR = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"'>]+)["']""", re.IGNORECASE)

_ATTR = re.compile(r"""\b([a-zA-Z-]+)\s*=\s*["']([^"']*)["']""")
_CSS_URL = re.compile(r"""url\(\s*['"]?(.*?)['"]?\s*\)""", re.IGNORECASE | re.DOTALL)
_CSS_IMPORT = re.compile(r"""@import\s+(?:url\()?\s*['"]([^'"]+)['"]""", re.IGNORECASE)
# Group 1 is the opening tag, group 2 the raw contents — the split matters
# because the opening tag can carry a `src` that must survive stripping.
_STYLE_BLOCK = re.compile(r"(<style\b[^>]*>)(.*?)</style\s*>", re.IGNORECASE | re.DOTALL)
_SCRIPT_BLOCK = re.compile(r"(<script\b[^>]*>)(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
_META_GENERATOR = re.compile(
    r"""<meta\b[^>]*?\bname\s*=\s*["']generator["'][^>]*?\bcontent\s*=\s*["']([^"']*)["']""",
    re.IGNORECASE,
)
_BASE_HREF = re.compile(r"""<base\b[^>]*?\bhref\s*=\s*["']([^"'>]+)["']""", re.IGNORECASE)
_TITLE = re.compile(r"<title\b[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)

# A CSS escape: backslash then one character that cannot start a hex escape.
# Enough for the \: and \/ Blogger emits; full CSS unescaping is not needed to
# recover the intended URL.
_CSS_ESCAPE = re.compile(r"\\([^0-9A-Fa-f\s])")

LAZY_ATTRIBUTES = ("data-src", "data-srcset", "data-original", "data-lazy-src", "data-bg")

# Feed types worth following, in the order we would prefer them.
_FEED_TYPES = ("application/atom+xml", "application/rss+xml", "application/feed+json")

_SKIP_SCHEMES = ("data:", "javascript:", "mailto:", "tel:", "about:", "blob:", "#")


def unescape_css(value: str) -> str:
    r"""Decode the backslash escapes a browser resolves in CSS."""
    return _CSS_ESCAPE.sub(r"\1", value)


def clean_attr_url(value: str) -> str:
    """An attribute value as the browser would see it."""
    return unescape_css(unescape(value)).strip()


def absolutize(base: str, value: str) -> str | None:
    """Resolve against the page, or None if it is not a fetchable http(s) URL."""
    if not value:
        return None
    candidate = value.strip()
    if not candidate or candidate.lower().startswith(_SKIP_SCHEMES):
        return None
    try:
        absolute = urljoin(base, candidate)
    except ValueError:
        return None
    if not absolute.startswith(("http://", "https://")):
        return None
    return absolute.split("#", 1)[0]


@dataclass(slots=True)
class PageRefs:
    """Everything one HTML page tells us about what it needs and where it points."""

    base: str
    links: set[str] = field(default_factory=set)
    assets: set[str] = field(default_factory=set)
    feeds: list[str] = field(default_factory=list)
    lazy_hints: int = 0
    generator: str | None = None
    title: str | None = None
    canonical: str | None = None
    next_page: str | None = None


def parse_page(body: bytes | str, url: str, *, want_links: bool = True) -> PageRefs:
    """Extract links, subresources, feeds and platform hints from one page."""
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    refs = PageRefs(base=url)

    base = url
    base_match = _BASE_HREF.search(text)
    if base_match:
        resolved = absolutize(url, clean_attr_url(base_match.group(1)))
        if resolved:
            base = resolved
            refs.base = resolved

    generator = _META_GENERATOR.search(text)
    if generator:
        refs.generator = unescape(generator.group(1)).strip()
    title = _TITLE.search(text)
    if title:
        refs.title = unescape(re.sub(r"\s+", " ", title.group(1))).strip()[:255]

    # <style> blocks are raw text: CSS escapes apply, HTML entities do not.
    for _open_tag, block in _STYLE_BLOCK.findall(text):
        for match in _CSS_URL.finditer(block):
            _add(refs.assets, base, unescape_css(match.group(1)))
        for match in _CSS_IMPORT.finditer(block):
            _add(refs.assets, base, unescape_css(match.group(1)))

    # Drop the *contents* of script and style elements while keeping their
    # opening tags. Removing the whole element takes `<script src=…>` with it,
    # so every external script silently vanishes from the results — which is
    # how an analytics host can be absent from the domain picker entirely.
    #
    # Script bodies are not parsed for URLs: guessing at strings inside
    # JavaScript produces exactly the kind of bogus request that wget's CSS
    # handling already demonstrates.
    outside = _SCRIPT_BLOCK.sub(r"\1", _STYLE_BLOCK.sub(r"\1", text))

    for pattern in (_ASSET_TAGS, _OBJECT_DATA, _IFRAME):
        for match in pattern.finditer(outside):
            _add(refs.assets, base, clean_attr_url(match.group(1)))

    for match in _SRCSET.finditer(outside):
        for candidate in match.group(1).split(","):
            # "url 2x" / "url 640w" — the descriptor is not part of the URL.
            _add(refs.assets, base, clean_attr_url(candidate.strip().split(" ")[0]))

    # Inline style="" IS an attribute, so entities are decoded there.
    for match in _CSS_URL.finditer(outside):
        _add(refs.assets, base, clean_attr_url(match.group(1)))

    refs.lazy_hints = sum(text.count(attr) for attr in LAZY_ATTRIBUTES)

    for tag in _LINK_TAG.finditer(outside):
        attrs = {k.lower(): v for k, v in _ATTR.findall(tag.group(0))}
        rel = (attrs.get("rel") or "").lower()
        href = clean_attr_url(attrs.get("href", ""))
        if not href:
            continue
        target = absolutize(base, href)
        if target is None:
            continue
        if "alternate" in rel and (attrs.get("type", "").lower() in _FEED_TYPES):
            if target not in refs.feeds:
                refs.feeds.append(target)
        elif rel in ("stylesheet", "icon", "shortcut icon", "apple-touch-icon", "preload"):
            refs.assets.add(target)
        elif rel == "canonical":
            refs.canonical = target
        elif rel == "next":
            refs.next_page = target

    if want_links:
        for match in _ANCHOR.finditer(outside):
            _add(refs.links, base, clean_attr_url(match.group(1)))

    return refs


def _add(into: set[str], base: str, raw: str) -> None:
    resolved = absolutize(base, raw)
    if resolved:
        into.add(resolved)


def referenced_assets(body: bytes, base_url: str) -> set[str]:
    """Just the subresources — what the post-capture audit compares against."""
    return parse_page(body, base_url, want_links=False).assets


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()
