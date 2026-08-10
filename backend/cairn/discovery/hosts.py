"""Host classification: grouping, roles, and the default selections (docs/04).

This produces the domain picker's table. The defaults matter more than the
classification: everything unknown starts *off*, which is what makes it
structurally impossible for a crawl to wander onto a neighbouring blog because
it appeared in a sidebar — the ArchiveBox failure this design exists to remove.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import tldextract

from cairn.discovery.platform import Preset, matches_host_pattern
from cairn.services.scope import ASSET_EXTENSIONS

# The bundled Public Suffix List snapshot, with no network access at runtime:
# a container that phones home on first use fails in exactly the air-gapped
# setups this tool is built for.
#
# include_psl_private_domains is the important half. The PSL's private section
# lists blogspot.com, github.io and friends as suffixes, so foo.blogspot.com
# and bar.blogspot.com come out as *different* registrable domains — which is
# the truth, they are different people's blogs. Without it both collapse to
# "blogspot.com" and the picker offers to crawl the neighbours as one group.
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)

ROLE_SELF = "self"
ROLE_IMAGES = "images"
ROLE_CDN = "cdn"
ROLE_FONTS = "fonts"
ROLE_ANALYTICS = "analytics"
ROLE_SOCIAL = "social"
ROLE_COMMENTS = "comments"
ROLE_ADS = "ads"
ROLE_UNKNOWN = "unknown"

# Hosts that only ever measure or monetize. Never worth archiving, and
# defaulting them off keeps the picker's unknown list short enough to read.
_ANALYTICS = (
    "google-analytics.com", "googletagmanager.com", "analytics.google.com",
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "scorecardresearch.com", "quantserve.com", "hotjar.com", "mixpanel.com",
    "segment.io", "segment.com", "amplitude.com", "matomo.cloud", "statcounter.com",
    "clarity.ms", "newrelic.com", "sentry.io", "facebook.net", "connect.facebook.net",
    "adservice.google.com", "amazon-adsystem.com", "criteo.com", "taboola.com",
    "outbrain.com", "pubmatic.com", "rubiconproject.com",
)  # fmt: skip

_SOCIAL = (
    "facebook.com", "twitter.com", "x.com", "platform.twitter.com", "instagram.com",
    "pinterest.com", "linkedin.com", "reddit.com", "tumblr.com", "addthis.com",
    "sharethis.com", "www.blogger.com",
)  # fmt: skip

_COMMENTS = ("disqus.com", "disquscdn.com", "commento.io", "hyvor.com", "utteranc.es")

_FONT_HOSTS = ("fonts.gstatic.com", "fonts.googleapis.com", "use.typekit.net", "fontawesome.com")

_IMAGE_HINTS = re.compile(r"(^|\.)(img|image|images|photo|photos|media|static|cdn|bp)\b")
# A host whose own name says it measures things. Catches the endless long tail
# of self-hosted and white-labelled analytics the blocklist will never contain.
_ANALYTICS_NAME = re.compile(r"^(analytics|telemetry|tracking|metrics|pixel|beacon)\.")
_IMAGE_TYPES = ("image/",)
_FONT_TYPES = ("font/", "application/font", "application/vnd.ms-fontobject")


@dataclass(slots=True)
class HostStat:
    """One row of the domain picker."""

    host: str
    registrable: str = ""
    is_seed_host: bool = False
    link_refs: int = 0
    asset_refs: int = 0
    distinct_urls: int = 0
    role: str = ROLE_UNKNOWN
    sample_urls: list[str] = field(default_factory=list)
    mime_types: dict[str, int] = field(default_factory=dict)
    crawl_pages: bool = False
    fetch_assets: bool = False
    allow_extensionless: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "registrable": self.registrable,
            "is_seed_host": self.is_seed_host,
            "link_refs": self.link_refs,
            "asset_refs": self.asset_refs,
            "distinct_urls": self.distinct_urls,
            "role": self.role,
            "sample_urls": self.sample_urls[:5],
            "crawl_pages": self.crawl_pages,
            "fetch_assets": self.fetch_assets,
            "allow_extensionless": self.allow_extensionless,
            "reason": self.reason,
        }


def registrable_domain(host: str) -> str:
    result = _EXTRACT(host)
    top = getattr(result, "top_domain_under_public_suffix", None)
    if top is None:  # pragma: no cover — older tldextract
        top = result.registered_domain
    return top or host


def guess_role(stat: HostStat, seed_host: str) -> str:
    host = stat.host.lower()
    registrable = stat.registrable.lower()

    if host == seed_host:
        return ROLE_SELF
    if _matches_any(host, registrable, _ANALYTICS) or _ANALYTICS_NAME.match(host):
        return ROLE_ANALYTICS
    if _matches_any(host, registrable, _COMMENTS):
        return ROLE_COMMENTS
    if _matches_any(host, registrable, _SOCIAL):
        return ROLE_SOCIAL
    if host in _FONT_HOSTS or any(t in stat.mime_types for t in _FONT_TYPES):
        return ROLE_FONTS

    images = sum(count for mime, count in stat.mime_types.items() if mime.startswith(_IMAGE_TYPES))
    total = sum(stat.mime_types.values())
    if total and images / total >= 0.6:
        return ROLE_IMAGES
    if stat.asset_refs and not stat.link_refs:
        return ROLE_IMAGES if _IMAGE_HINTS.search(host) else ROLE_CDN
    return ROLE_UNKNOWN


def _matches_any(host: str, registrable: str, needles: tuple[str, ...]) -> bool:
    return any(host == n or registrable == n or host.endswith(f".{n}") for n in needles)


def classify(
    *,
    seed_host: str,
    link_refs: dict[str, int],
    asset_refs: dict[str, int],
    urls_by_host: dict[str, set[str]],
    mime_by_host: dict[str, dict[str, int]] | None = None,
) -> list[HostStat]:
    """Build the picker rows from the counts discovery gathered."""
    mime_by_host = mime_by_host or {}
    stats: list[HostStat] = []

    for host in sorted({*link_refs, *asset_refs, *urls_by_host}):
        if not host:
            continue
        stat = HostStat(
            host=host,
            registrable=registrable_domain(host),
            is_seed_host=host == seed_host,
            link_refs=link_refs.get(host, 0),
            asset_refs=asset_refs.get(host, 0),
            distinct_urls=len(urls_by_host.get(host, ())),
            sample_urls=sorted(urls_by_host.get(host, ()))[:5],
            mime_types=mime_by_host.get(host, {}),
        )
        stat.role = guess_role(stat, seed_host)
        stats.append(stat)

    # Most-referenced first, seed host always at the top: the table is read
    # top-down and the rows that need a decision should be the ones in view.
    stats.sort(key=lambda s: (not s.is_seed_host, -(s.link_refs + s.asset_refs), s.host))
    return stats


def apply_defaults(stats: list[HostStat], preset: Preset | None = None) -> list[HostStat]:
    """Preselect so the common case needs zero clicks (docs/04).

    Unknown hosts default to *off*. That is the whole point: a crawl cannot
    reach a neighbouring blog that was merely linked, because nothing turns it
    on by accident.
    """
    for stat in stats:
        if stat.is_seed_host:
            stat.crawl_pages = True
            stat.fetch_assets = True
            stat.reason = "the site being archived"
            continue

        if preset and any(matches_host_pattern(p, stat.host) for p in preset.hosts_off):
            stat.crawl_pages = stat.fetch_assets = False
            stat.reason = f"excluded by the {preset.name} preset"
            continue

        if preset and any(matches_host_pattern(p, stat.host) for p in preset.assets_on):
            stat.fetch_assets = True
            stat.allow_extensionless = any(
                matches_host_pattern(p, stat.host) for p in preset.extensionless_ok
            )
            stat.reason = f"assets for {preset.name}"
            continue

        if stat.role in (ROLE_ANALYTICS, ROLE_ADS):
            stat.reason = "analytics — nothing to archive"
            continue
        if stat.role in (ROLE_SOCIAL, ROLE_COMMENTS):
            stat.reason = f"{stat.role} widget — usually not archivable offline"
            continue

        if stat.asset_refs and not stat.link_refs:
            stat.fetch_assets = True
            stat.reason = "only ever referenced as a subresource"
            continue

        stat.reason = "not referenced as a subresource — review before enabling"

    return stats


def group_by_registrable(stats: list[HostStat]) -> dict[str, list[HostStat]]:
    """For the picker's grouped view."""
    grouped: dict[str, list[HostStat]] = defaultdict(list)
    for stat in stats:
        grouped[stat.registrable].append(stat)
    return dict(grouped)


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


_ASSET_PATH = re.compile(rf"\.({ASSET_EXTENSIONS})$", re.IGNORECASE)


def looks_like_asset(url: str) -> bool:
    """Whether an `<a href>` points at a file rather than a page.

    Uses the same extension list the scope translation uses, so the picker and
    the crawler agree about what counts as an asset.
    """
    return bool(_ASSET_PATH.search(urlsplit(url).path))
