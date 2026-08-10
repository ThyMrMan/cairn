"""The resolved scope object and its translation to crawler flags.

Scope is engine-independent (docs/04): it is stored on the site, passed
verbatim into every job spec, and copied into `manifest.json` so a capture's
boundary stays auditable years later. Engines translate it; nothing else
interprets it.

The awkward part is `crawl_pages: false` — "fetch this host's images but do
not crawl it as a website." wget has no such concept, so it is synthesized
from `--domains` plus a reject regex. Three things were established against
the real wget 1.25.0 in the container image, and all three constrain what this
module may generate:

  1. A reject regex that blocks pages on an asset host still lets
     `--page-requisites` pull that host's assets. The translation works.
  2. `--domains` is a hard gate that `--page-requisites` does NOT bypass.
     Omitting an asset host from `--domains` drops its images entirely, so
     asset-only hosts must be listed *and* fenced by the regex. There is no
     regex-free formulation.
  3. No regex over URLs can separate an extension-less image from an
     extension-less page, because the URL text is identical. The allowlist
     therefore drops Blogger's proxied images
     (lh3.googleusercontent.com/blogger_img_proxy/AEn0k_…) unless a host opts
     in via `allow_extensionless`. Defaulting that on would let wget crawl a
     CDN's HTML; defaulting it off loses images silently. It defaults off and
     the Blogger preset turns it on for the hosts where it is known safe —
     and `missing_assets` reporting after a capture catches whatever slips
     through either way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

SCOPE_SCHEMA_VERSION = 1

# Extensions treated as assets on a host that is not being crawled. Generous
# on purpose: a false positive costs one unnecessary fetch, a false negative
# costs an image that is missing from the archive forever.
ASSET_EXTENSIONS = (
    "jpe?g|png|gif|webp|avif|svg|ico|bmp|tiff?"
    "|css|js|mjs|cjs|map"
    "|woff2?|ttf|otf|eot"
    "|mp4|webm|ogg|ogv|mp3|wav|m4a|flac"
    "|pdf|zip|gz"
)

DEFAULT_POLITENESS: dict[str, Any] = {
    "wait_s": 1.0,
    "random_wait": True,
    "rate_limit": "2m",
    "concurrency": 1,
}


class ScopeError(ValueError):
    """The scope object is internally inconsistent or unusable."""


@dataclass(slots=True)
class HostRule:
    """One host's crawl decision — the two checkboxes from the domain picker.

    `crawl_pages` and `fetch_assets` are independent by design; collapsing
    them into one flag is the mistake docs/03 calls out.
    """

    host: str
    crawl_pages: bool = False
    fetch_assets: bool = True
    path_prefix: str | None = None
    # Permit extension-less URLs on an assets-only host. Only safe where the
    # host is known to serve images and not linked HTML; see the module
    # docstring for why this cannot be inferred from the URL.
    allow_extensionless: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "crawl_pages": self.crawl_pages,
            "fetch_assets": self.fetch_assets,
            "path_prefix": self.path_prefix,
            "allow_extensionless": self.allow_extensionless,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> HostRule:
        host = str(raw.get("host", "")).strip().lower()
        if not host:
            raise ScopeError("a host rule has no host")
        return cls(
            host=host,
            crawl_pages=bool(raw.get("crawl_pages", False)),
            fetch_assets=bool(raw.get("fetch_assets", True)),
            path_prefix=raw.get("path_prefix") or None,
            allow_extensionless=bool(raw.get("allow_extensionless", False)),
        )


@dataclass(slots=True)
class Scope:
    """A capture's complete boundary."""

    seeds: list[str] = field(default_factory=list)
    hosts: list[HostRule] = field(default_factory=list)
    exclude_hosts: list[str] = field(default_factory=list)
    accept_patterns: list[str] = field(default_factory=list)
    reject_patterns: list[str] = field(default_factory=list)
    seed_urls_from: dict[str, bool] = field(
        default_factory=lambda: {"sitemap": True, "feeds": True}
    )
    path_prefix: str | None = None
    max_depth: int | None = None
    max_pages: int | None = None
    max_bytes: int | None = None
    obey_robots: bool = True
    politeness: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_POLITENESS))

    # ── serialization ────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCOPE_SCHEMA_VERSION,
            "seeds": list(self.seeds),
            "seed_urls_from": dict(self.seed_urls_from),
            "hosts": [h.to_dict() for h in self.hosts],
            "exclude_hosts": list(self.exclude_hosts),
            "path_prefix": self.path_prefix,
            "accept_patterns": list(self.accept_patterns),
            "reject_patterns": list(self.reject_patterns),
            "max_depth": self.max_depth,
            "max_pages": self.max_pages,
            "max_bytes": self.max_bytes,
            "obey_robots": self.obey_robots,
            "politeness": dict(self.politeness),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Scope:
        politeness = dict(DEFAULT_POLITENESS)
        politeness.update(raw.get("politeness") or {})
        return cls(
            seeds=[str(s) for s in raw.get("seeds") or []],
            hosts=[HostRule.from_dict(h) for h in raw.get("hosts") or []],
            exclude_hosts=[str(h).lower() for h in raw.get("exclude_hosts") or []],
            accept_patterns=[str(p) for p in raw.get("accept_patterns") or []],
            reject_patterns=[str(p) for p in raw.get("reject_patterns") or []],
            seed_urls_from=dict(raw.get("seed_urls_from") or {"sitemap": True, "feeds": True}),
            path_prefix=raw.get("path_prefix") or None,
            max_depth=raw.get("max_depth"),
            max_pages=raw.get("max_pages"),
            max_bytes=raw.get("max_bytes"),
            obey_robots=bool(raw.get("obey_robots", True)),
            politeness=politeness,
        )

    # ── queries ──────────────────────────────────────────────────────────

    @property
    def crawl_hosts(self) -> list[HostRule]:
        return [h for h in self.hosts if h.crawl_pages]

    @property
    def asset_only_hosts(self) -> list[HostRule]:
        return [h for h in self.hosts if h.fetch_assets and not h.crawl_pages]

    @property
    def allowed_hosts(self) -> list[str]:
        """Every host wget may contact at all — see finding 2 above."""
        return [h.host for h in self.hosts if h.crawl_pages or h.fetch_assets]

    def validate(self) -> None:
        if not self.seeds:
            raise ScopeError("scope has no seed URLs")
        for seed in self.seeds:
            parts = urlsplit(seed)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ScopeError(f"seed is not an absolute http(s) URL: {seed!r}")
        if not self.allowed_hosts:
            raise ScopeError("scope allows no hosts; the capture would fetch nothing")
        if not self.crawl_hosts:
            raise ScopeError(
                "no host is marked crawlable; at least the seed host must have "
                "'Crawl' enabled or only its assets would be fetched"
            )
        seen: set[str] = set()
        for rule in self.hosts:
            if rule.host in seen:
                raise ScopeError(f"duplicate host rule for {rule.host!r}")
            seen.add(rule.host)
        overlap = seen & set(self.exclude_hosts)
        if overlap:
            raise ScopeError(f"host is both included and excluded: {', '.join(sorted(overlap))}")
        for pattern in (*self.accept_patterns, *self.reject_patterns):
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ScopeError(f"invalid pattern {pattern!r}: {exc}") from exc


# ── defaults from a seed URL ─────────────────────────────────────────────


def default_scope(seed_url: str) -> Scope:
    """The scope a site gets before discovery has run.

    Seed host only, crawl + assets. Deliberately narrow: an unscoped default
    that spans hosts is how a crawl ends up on a neighbouring blog, which is
    the ArchiveBox failure this design exists to make unreachable (docs/04).
    """
    parts = urlsplit(seed_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ScopeError(f"seed is not an absolute http(s) URL: {seed_url!r}")
    return Scope(
        seeds=[seed_url],
        hosts=[HostRule(host=parts.hostname.lower(), crawl_pages=True, fetch_assets=True)],
    )


# ── regex construction ───────────────────────────────────────────────────


def _host_pattern(host: str) -> str:
    """Regex-quote a host, allowing an optional port.

    Hosts come from discovery and from user input; they reach a regex that
    reaches a subprocess argument. A host containing regex metacharacters
    must not change the pattern's meaning.
    """
    return re.escape(host) + r"(?::[0-9]+)?"


def asset_only_reject_pattern(rule: HostRule) -> str:
    """Block page URLs on a host whose assets we want but whose pages we don't.

    Reads as: on this host, reject anything that is not a known asset. The
    negative lookahead needs PCRE — POSIX ERE has none, which is why the
    engine passes `--regex-type=pcre` and the image verifies at build time
    that its wget can compile one (docs/04).
    """
    host = _host_pattern(rule.host)
    tail = rf"(?!.*\.(?:{ASSET_EXTENSIONS})(?:$|\?))"
    if rule.allow_extensionless:
        # Also permit paths with no extension at all. Widens the archive to
        # image-proxy URLs at the cost of admitting extension-less HTML.
        tail = rf"(?!.*\.(?:{ASSET_EXTENSIONS})(?:$|\?))(?=.*\.[A-Za-z0-9]{{1,8}}(?:$|\?))"
    return rf"^https?://{host}/{tail}"


def build_reject_patterns(scope: Scope) -> list[str]:
    """Every reject pattern the engine should enforce, user's plus generated."""
    patterns = list(scope.reject_patterns)
    patterns.extend(asset_only_reject_pattern(rule) for rule in scope.asset_only_hosts)
    return patterns


def combine_patterns(patterns: list[str]) -> str:
    """Join alternatives into one regex.

    Each branch is wrapped so that a pattern with a top-level alternation —
    `[?&]m=1|foo` — cannot escape its own branch and swallow the next one.
    """
    return "|".join(f"(?:{p})" for p in patterns)


# ── wget translation ─────────────────────────────────────────────────────


@dataclass(slots=True)
class WgetScopeArgs:
    """The scope-derived portion of a wget command line."""

    args: list[str]
    notes: list[str] = field(default_factory=list)


def to_wget_args(scope: Scope, *, regex_type: Literal["pcre", "posix"] = "pcre") -> WgetScopeArgs:
    """Translate a resolved scope into wget flags (docs/04).

    Returns only the scope-derived flags; politeness, WARC output and auth are
    the engine's business. `notes` carries anything the user should be told
    about how their selection was interpreted.
    """
    scope.validate()
    args: list[str] = ["--span-hosts", f"--domains={','.join(scope.allowed_hosts)}"]
    notes: list[str] = []

    if scope.exclude_hosts:
        args.append(f"--exclude-domains={','.join(scope.exclude_hosts)}")

    rejects = build_reject_patterns(scope)
    if rejects:
        if regex_type == "posix":
            raise ScopeError(
                "asset-only hosts need a negative lookahead, which POSIX ERE does not "
                "support. Pass --regex-type=pcre, or mark those hosts crawlable."
            )
        args += ["--regex-type=pcre", f"--reject-regex={combine_patterns(rejects)}"]

    if scope.accept_patterns:
        # wget takes one --accept-regex; a second silently replaces the first.
        args += [f"--accept-regex={combine_patterns(scope.accept_patterns)}"]
        if "--regex-type=pcre" not in args:
            args.insert(len(args) - 1, "--regex-type=pcre")

    if scope.path_prefix:
        args.append("--no-parent")
        notes.append(f"Restricted to paths under {scope.path_prefix}.")

    args.append("--level=inf" if scope.max_depth is None else f"--level={scope.max_depth}")

    if scope.max_bytes:
        args.append(f"--quota={scope.max_bytes}")
    if not scope.obey_robots:
        args += ["-e", "robots=off"]
        notes.append("robots.txt is being ignored for this capture.")

    for rule in scope.asset_only_hosts:
        if not rule.allow_extensionless:
            notes.append(
                f"{rule.host} is assets-only, so URLs there without a file extension "
                f"are skipped. Enable 'allow extension-less' if it serves images "
                f"through extension-less URLs."
            )

    if scope.max_pages is not None:
        # wget has no page cap; the supervisor enforces it by counting `url`
        # events and cancelling. Recorded so the manifest stays honest.
        notes.append(f"Page cap of {scope.max_pages} is enforced by the supervisor, not wget.")

    return WgetScopeArgs(args=args, notes=notes)
