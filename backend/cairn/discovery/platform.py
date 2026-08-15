"""Platform fingerprinting and the presets it unlocks (docs/04 phase 1).

Recognising the platform is most of the value of probing the origin. A preset
supplies the right sitemap and feed paths, the right junk-parameter rejects,
and the right asset hosts — turning a domain picker that needs thought into
one that needs zero clicks for the common case.

The Blogger preset's host lists are not guesses. They come from the gap report
of a real capture, which named exactly what a Blogger page pulls from
elsewhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

BLOGGER = "blogger"
WORDPRESS = "wordpress"
GHOST = "ghost"
SUBSTACK = "substack"
SQUARESPACE = "squarespace"
UNKNOWN = "unknown"


@dataclass(slots=True)
class Preset:
    id: str
    name: str
    assets_on: list[str] = field(default_factory=list)
    hosts_off: list[str] = field(default_factory=list)
    extensionless_ok: list[str] = field(default_factory=list)
    reject_patterns: list[tuple[str, str]] = field(default_factory=list)
    # Patterns this preset used to ship and no longer stands behind.
    #
    # Applying a preset merges patterns *in*, which is right — it must never
    # discard something added by hand. But it meant a preset could only ever
    # grow, so correcting a pattern reached new sites and never existing ones:
    # the wrong rule stayed in every scope that already had it, with no way to
    # tell it from a deliberate choice. Naming the retired ones is what makes
    # a correction propagate.
    retired_patterns: list[str] = field(default_factory=list)
    sitemap_paths: tuple[str, ...] = ()
    feed_paths: tuple[str, ...] = ()
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "assets_on": self.assets_on,
            "hosts_off": self.hosts_off,
            "extensionless_ok": self.extensionless_ok,
            "reject_patterns": [{"pattern": p, "note": n} for p, n in self.reject_patterns],
            "notes": self.notes,
        }


BLOGGER_PRESET = Preset(
    id=BLOGGER,
    name="Blogger / Blogspot",
    assets_on=[
        "*.bp.blogspot.com",
        "blogger.googleusercontent.com",
        "lh3.googleusercontent.com",
        "*.ggpht.com",
        "fonts.gstatic.com",
        # The theme's compiled CSS and JS. Its absence is visible in replay.
        "resources.blogblog.com",
        "www.blogblog.com",
        # Skin background images, reached through CSS-escaped URLs that wget
        # mangles — in scope so at least the scope half is right.
        "themes.googleusercontent.com",
        "www.gstatic.com",
        # Serves both the theme's own widgets.js — which the page runs, and
        # whose absence shows up as console errors in replay — and two things
        # worth nothing, rejected by pattern below rather than by dropping the
        # whole host.
        "www.blogger.com",
    ],
    hosts_off=[
        "*.google-analytics.com",
        "*.googletagmanager.com",
        "*.doubleclick.net",
    ],
    # Every one of these serves images through URLs with no file extension, so
    # without this the assets-only reject regex drops them silently — the case
    # the scope module's third finding is about. themes.googleusercontent.com
    # is the one that bites: its skin images are all `image?id=…`.
    extensionless_ok=[
        "blogger.googleusercontent.com",
        "lh3.googleusercontent.com",
        "themes.googleusercontent.com",
    ],
    reject_patterns=[
        (r"[?&]m=1", "the mobile duplicate of every page — halves the crawl, loses nothing"),
        (r"[?&]replytocom=", "one permutation per comment reply"),
        (r"[?&]showComment=", "comment anchors"),
        # Pagination *within a label*, which is the combination that multiplies:
        # one chain per label, and a blog with 60 labels has 60 of them. The
        # plain `/search?updated-max=` chain is deliberately not rejected — it
        # is the blog's own "Older posts" trail, it is bounded at roughly one
        # page per five posts, and rejecting it made that link dead in replay
        # for no saving worth having. Reported from a 43-post blog whose
        # archive had a broken "Older posts" at the bottom of every page.
        (
            r"/search/label/[^?]*\?[^#]*updated-(max|min)=",
            "archive pagination repeated once per label, which is where it "
            "multiplies; the blog's own Older-posts trail is left alone",
        ),
        (r"\?action=backlinks", "backlink stubs"),
        (
            r"^https?://www\.blogger\.com/dyn-css/",
            "the owner's admin-bar CSS, cache-busted with a fresh zx= on every "
            "page load — a new URL each time, so it is one extra fetch per page "
            "and no two of them are shared",
        ),
        (
            r"^https?://www\.blogger\.com/[^?]*comment[_-]from[_-]post[_-]iframe",
            "the comment iframe bootstrap; the iframe it builds cannot work offline",
        ),
        # The five below were measured on a real 43-post blog captured with a
        # browser engine, which fetches everything a page asks for rather than
        # only what wget can see in the markup. They came to roughly half of
        # every request made and about 2.8 MB, and not one of them can do
        # anything in a replayed page.
        (
            r"/b/stats\?",
            "the view-counter beacon, fired on every page load — 22% of all "
            "requests on the blog this was measured against, and it reports a "
            "view to a server that is not there",
        ),
        (
            r"^https?://(?:www\.)?google\.com/recaptcha/",
            "the comment form's captcha; it cannot be solved offline and the "
            "form it guards cannot submit anywhere",
        ),
        (
            r"^https?://www\.blogger\.com/navbar/",
            "the owner's admin navbar iframe — one per page, and signed out in "
            "an archive even for the owner",
        ),
        (
            r"^https?://www\.blogger\.com/comment/frame/",
            "the comment iframe itself, which posts to a live endpoint. The "
            "comments already in the page are part of the page and are kept",
        ),
        (
            r"/feeds/posts/default\?[^#]*callback=",
            "the JSONP form of the feed, called once per page by widgets. The "
            "plain feed is left alone — discovery reads it",
        ),
    ],
    # Shipped until the Older-posts trail turned out to be a dead link in the
    # archive rather than an infinite loop. Sites that already have it keep
    # blocking their own pagination until the preset is applied again.
    retired_patterns=[r"/search\?updated-(max|min)="],
    sitemap_paths=("/sitemap.xml",),
    feed_paths=("/feeds/posts/default",),
    notes=(
        "Blogger serves every post twice — the desktop URL and a ?m=1 mobile "
        "duplicate that most themes link to in the footer. Rejecting it halves "
        "the crawl with no content loss.\n\n"
        "Everything under /search is robots-disallowed, and that is one switch "
        "covering two very different things. Label pages (/search/label/X) are "
        "one per label and each re-lists posts you already have — a 43-post "
        "blog produced 115 of them. The Older-posts trail (/search?updated-max=) "
        "is the blog's own pagination, about one page per five posts, and "
        "without it that link is dead in the archive.\n\n"
        "So: turn off 'obey robots.txt' to get the Older-posts trail. Label "
        "pages come with it — add a reject for /search/label/ in this site's "
        "patterns if you do not want them. Pagination *inside* a label is "
        "rejected either way, since that is the combination that multiplies."
    ),
)

WORDPRESS_PRESET = Preset(
    id=WORDPRESS,
    name="WordPress",
    assets_on=["*.wp.com", "secure.gravatar.com", "*.gravatar.com", "fonts.gstatic.com"],
    hosts_off=["*.google-analytics.com", "*.googletagmanager.com", "stats.wp.com"],
    reject_patterns=[
        (r"[?&]replytocom=", "one permutation per comment reply"),
        (r"/wp-json/", "the REST API duplicates every post as JSON"),
        (r"\?share=", "share-link permutations"),
    ],
    sitemap_paths=("/wp-sitemap.xml", "/sitemap_index.xml", "/sitemap.xml"),
    feed_paths=("/feed", "/feed/atom"),
    notes=(
        "WordPress exposes every post again under /wp-json; rejecting it avoids "
        "archiving each one twice."
    ),
)

GHOST_PRESET = Preset(
    id=GHOST,
    name="Ghost",
    assets_on=["*.gravatar.com", "fonts.gstatic.com"],
    hosts_off=["*.google-analytics.com"],
    sitemap_paths=("/sitemap.xml",),
    feed_paths=("/rss/",),
)

SUBSTACK_PRESET = Preset(
    id=SUBSTACK,
    name="Substack",
    assets_on=["substackcdn.com", "*.substackcdn.com", "substack-post-media.s3.amazonaws.com"],
    hosts_off=["*.google-analytics.com"],
    extensionless_ok=["substackcdn.com", "*.substackcdn.com"],
    sitemap_paths=("/sitemap.xml",),
    feed_paths=("/feed",),
)

SQUARESPACE_PRESET = Preset(
    id=SQUARESPACE,
    name="Squarespace",
    assets_on=[
        # Every uploaded image goes through this, on every template.
        "images.squarespace-cdn.com",
        "*.squarespace-cdn.com",
        # Template CSS/JS and uploaded files that are not images.
        "static1.squarespace.com",
        "assets.squarespace.com",
        # Templates pull webfonts from both, and both halves are needed: one
        # serves the @font-face CSS, the other the font files. A page missing
        # either falls back to a system font in replay.
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "use.typekit.net",
        "p.typekit.net",
    ],
    hosts_off=[
        "*.google-analytics.com",
        "*.googletagmanager.com",
        "*.doubleclick.net",
    ],
    # Deliberately empty. Squarespace image URLs keep the source file's
    # extension ahead of the query string — `…/photo.jpg?format=2500w` — and
    # the asset pattern already allows an extension followed by `?`, so they
    # are matched without it. Turning it on would let a crawl follow HTML on
    # the CDN for no gain; anything that does slip through shows up in
    # `missing_assets` after a capture.
    extensionless_ok=[],
    reject_patterns=[
        (
            r"[?&]format=json(-pretty)?(&|$)",
            "the whole page again as JSON — Squarespace's equivalent of "
            "WordPress's /wp-json/, and every page has one",
        ),
    ],
    sitemap_paths=("/sitemap.xml",),
    # Squarespace has no site-wide feed: each blog collection publishes its own
    # at `<collection>?format=rss`, so there is no path to guess that is right
    # for every site. These are the three usual collection names, tried after
    # the page's own <link rel="alternate">, which is where a correct answer
    # normally comes from.
    feed_paths=("/blog?format=rss", "/news?format=rss", "/journal?format=rss"),
    notes=(
        "Every page also answers on ?format=json, which is the same content "
        "again in a form nothing replays. That is rejected.\n\n"
        "Blog collections paginate with ?offset=<timestamp> and filter with "
        "?tag=, ?category=, ?author= and ?month=. None of those are rejected "
        "here, for the reason the Blogger preset leaves the Older-posts trail "
        "alone: ?offset= is the blog's own pagination and rejecting it makes "
        "that link dead in the archive, and the filter pages are real "
        "navigation. If a capture shows tag and category pages dominating the "
        "fetch list, add a reject for [?&]tag= to this site's patterns — the "
        "same trade as Blogger's label pages.\n\n"
        "The hosts and paths above are Squarespace's documented infrastructure. "
        "Unlike the Blogger preset, the reject list has not been measured "
        "against a real capture — if you archive a Squarespace site, the "
        "'what it fetched' list is what would turn this into a preset that "
        "pulls its weight."
    ),
)

PRESETS: dict[str, Preset] = {
    p.id: p
    for p in (
        BLOGGER_PRESET,
        WORDPRESS_PRESET,
        GHOST_PRESET,
        SUBSTACK_PRESET,
        SQUARESPACE_PRESET,
    )
}


@dataclass(slots=True)
class Fingerprint:
    platform: str = UNKNOWN
    confidence: str = "none"  # strong | weak | none
    evidence: list[str] = field(default_factory=list)

    @property
    def preset(self) -> Preset | None:
        return PRESETS.get(self.platform)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "preset": self.preset.to_dict() if self.preset else None,
        }


_GENERATOR_HINTS = (
    (BLOGGER, re.compile(r"\bblogger\b", re.IGNORECASE)),
    (WORDPRESS, re.compile(r"\bwordpress\b", re.IGNORECASE)),
    (GHOST, re.compile(r"\bghost\b", re.IGNORECASE)),
    (SQUARESPACE, re.compile(r"\bsquarespace\b", re.IGNORECASE)),
)

_HOST_HINTS = (
    (BLOGGER, re.compile(r"\.blogspot\.(com|[a-z.]+)$", re.IGNORECASE)),
    (SUBSTACK, re.compile(r"\.substack\.com$", re.IGNORECASE)),
    (WORDPRESS, re.compile(r"\.wordpress\.com$", re.IGNORECASE)),
    (GHOST, re.compile(r"\.ghost\.io$", re.IGNORECASE)),
    (SQUARESPACE, re.compile(r"\.squarespace\.com$", re.IGNORECASE)),
)

_BODY_HINTS = (
    (BLOGGER, re.compile(rb"blogger\.com/(?:static|dyn-css)|blogblog\.com|_WidgetManager")),
    (WORDPRESS, re.compile(rb"/wp-content/|/wp-includes/")),
    (GHOST, re.compile(rb"ghost-sdk|/ghost/api/")),
    (SUBSTACK, re.compile(rb"substackcdn\.com|substack\.com/api")),
    # `SQUARESPACE_CONTEXT` is the config blob injected into every page; the
    # CDN hosts catch templates that render it differently. Custom domains are
    # the whole point here — a site on its own domain has nothing in the
    # hostname, and those are the ones worth presetting.
    (SQUARESPACE, re.compile(rb"SQUARESPACE_CONTEXT|squarespace-cdn\.com|squarespace\.com/")),
)


def fingerprint(
    *,
    url: str,
    body: bytes | None = None,
    generator: str | None = None,
    headers: dict[str, str] | None = None,
) -> Fingerprint:
    """Identify the platform from the generator tag, the host, and the body.

    A custom-domain Blogger blog has no `.blogspot.com` in its URL, so the host
    check alone misses exactly the blogs most worth presetting. The body check
    is what catches those.
    """
    result = Fingerprint()
    host = (urlsplit(url).hostname or "").lower()

    if generator:
        for platform, pattern in _GENERATOR_HINTS:
            if pattern.search(generator):
                result.platform = platform
                result.confidence = "strong"
                result.evidence.append(f"generator meta tag says {generator!r}")
                return result

    for platform, pattern in _HOST_HINTS:
        if pattern.search(host):
            result.platform = platform
            result.confidence = "strong"
            result.evidence.append(f"hostname {host} belongs to {platform}")
            return result

    for header, value in (headers or {}).items():
        if header.lower() == "x-powered-by" and "wordpress" in value.lower():
            result.platform = WORDPRESS
            result.confidence = "weak"
            result.evidence.append(f"X-Powered-By: {value}")
            return result

    if body:
        for platform, body_pattern in _BODY_HINTS:
            if body_pattern.search(body):
                result.platform = platform
                result.confidence = "weak"
                result.evidence.append(f"page markup matches {platform}")
                return result

    return result


def matches_host_pattern(pattern: str, host: str) -> bool:
    """`*.bp.blogspot.com` against a host. Only a leading `*.` is meaningful.

    Deliberately not a general glob: `*` in the middle of a hostname pattern
    invites a rule that matches far more than its author intended, and every
    real case here is a subdomain wildcard.
    """
    host = host.lower().strip(".")
    pattern = pattern.lower().strip(".")
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return host == suffix or host.endswith(f".{suffix}")
    return host == pattern
