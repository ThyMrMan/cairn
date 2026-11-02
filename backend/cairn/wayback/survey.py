"""What does the Wayback Machine hold of this site? (docs/17 phase 1)

Reads the CDX pages `cdx.pull` saved and reduces them, with no network, to:

- `survey.json`: the coverage report the UI shows.
- `selection.jsonl.gz`: one line per URL the import would fetch, with the
  snapshot chosen and a few older fallbacks. Stage 2's engine reads this.

Four rules shape the reduction, each from the fictionmania survey:

1. **The site's own scope applies.** Hosts, path prefix, accept and reject
   patterns, the instance skip list. A pattern that drops 40,000 review pages
   is reported with its count, so its cost is visible.
2. **URL groups collapse views of one item.** A story served under five URL
   shapes is one story; the import fetches the preferred view (plus any the
   group says to keep), not all five.
3. **Error pages are not snapshots.** Digests of the site's rate-limit page,
   and any digest shared by many different HTML pages, are boilerplate the
   archive captured in place of content. The newest snapshot that is *not*
   one of those wins.
4. **The newest good snapshot wins**, with older ones kept as fallbacks.
"""

from __future__ import annotations

import gzip
import heapq
import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cairn.logging import get_logger
from cairn.services import storage
from cairn.services.scope import Scope
from cairn.wayback import cdx

log = get_logger(__name__)

SURVEY_FILE = "survey.json"
SELECTION_FILE = "selection.jsonl.gz"
CDX_DIR = "cdx"
# Snapshots kept per URL: the chosen one and fallbacks if it proves bad.
KEEP_SNAPSHOTS = 4
# An HTML digest shared by this many different URLs is boilerplate. The two
# found on fictionmania covered 33 and 48 story URLs; real pages are unique.
SHARED_DIGEST_MIN_URLS = 20
HTML_TYPES = ("text/html", "application/xhtml+xml")
REPORT_VERSION = 1


class SurveyError(Exception):
    pass


# ── URL groups ───────────────────────────────────────────────────────────


@dataclass(slots=True)
class UrlGroup:
    """Several URL shapes that serve one item (docs/19, decision 1).

    `pattern` must name an `item` group; with `prefer` or `keep` it must also
    name a `view` group. Matching ignores case, because the CDX key is
    lowercased and the original URL is not.
    """

    name: str
    pattern: re.Pattern[str]
    prefer: list[str] = field(default_factory=list)
    keep: list[str] = field(default_factory=list)

    def match(self, url: str) -> tuple[str, str] | None:
        m = self.pattern.search(url)
        if m is None:
            return None
        view = (m.groupdict().get("view") or "").lower()
        return m.group("item"), view

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "pattern": self.pattern.pattern,
            "prefer": list(self.prefer),
            "keep": list(self.keep),
        }


def parse_groups(raw: Iterable[dict[str, Any]] | None) -> list[UrlGroup]:
    groups: list[UrlGroup] = []
    names: set[str] = set()
    for entry in raw or []:
        name = str(entry.get("name") or "").strip()
        if not name:
            raise ValueError("every URL group needs a name")
        if name in names:
            raise ValueError(f"two URL groups are called {name!r}")
        names.add(name)
        try:
            pattern = re.compile(str(entry.get("pattern") or ""), re.I)
        except re.error as exc:
            raise ValueError(f"URL group {name!r}: invalid pattern: {exc}") from exc
        if "item" not in pattern.groupindex:
            raise ValueError(f"URL group {name!r}: the pattern needs a (?P<item>…) group")
        prefer = [str(v).lower() for v in entry.get("prefer") or [] if str(v).strip()]
        keep = [str(v).lower() for v in entry.get("keep") or [] if str(v).strip()]
        if (prefer or keep) and "view" not in pattern.groupindex:
            raise ValueError(
                f"URL group {name!r}: 'prefer' and 'keep' need a (?P<view>…) group in the pattern"
            )
        groups.append(UrlGroup(name=name, pattern=pattern, prefer=prefer, keep=keep))
    return groups


# ── scope ────────────────────────────────────────────────────────────────


class ScopeFilter:
    """The site's scope, applied to archived URLs rather than live ones."""

    def __init__(self, scope: Scope) -> None:
        self.page_hosts: dict[str, str | None] = {}
        self.asset_hosts: set[str] = set()
        for rule in scope.hosts:
            key = cdx.surt_host(rule.host)
            if rule.crawl_pages:
                self.page_hosts[key] = rule.path_prefix
            elif rule.fetch_assets:
                self.asset_hosts.add(key)
        self.path_prefix = scope.path_prefix
        self.rejects = [(p, re.compile(p)) for p in scope.all_reject_patterns if _valid(p)]
        self.accepts = [re.compile(p) for p in scope.accept_patterns if _valid(p)]

    @property
    def crawl_hosts(self) -> list[str]:
        return sorted(self.page_hosts)

    def classify(self, url: str, mimetype: str) -> tuple[str | None, str]:
        """("page"|"asset", "") when in scope, else (None, reason)."""
        parts = urlsplit(url)
        host = cdx.surt_host(parts.hostname or "")
        is_html = mimetype in HTML_TYPES
        path = parts.path or "/"
        if host in self.page_hosts:
            for prefix in (self.page_hosts[host], self.path_prefix):
                if prefix and is_html and not path.startswith(prefix):
                    return None, "outside the path prefix"
        elif host in self.asset_hosts:
            if is_html:
                return None, "page on an assets-only host"
        else:
            return None, "host not in scope"
        for pattern, rx in self.rejects:
            if rx.search(url):
                return None, f"reject: {pattern}"
        if self.accepts and not any(rx.search(url) for rx in self.accepts):
            return None, "matches no accept pattern"
        return ("page" if is_html else "asset"), ""


def _valid(pattern: str) -> bool:
    try:
        re.compile(pattern)
    except re.error:
        return False
    return True


# ── reduction ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Url:
    original: str
    kind: str
    mimetype: str
    newest_ts: str = ""
    # Min-heap of (timestamp, digest, length): the newest KEEP_SNAPSHOTS.
    snaps: list[tuple[str, str, int | None]] = field(default_factory=list)


@dataclass(slots=True)
class Selected:
    key: str
    url: str
    kind: str
    mimetype: str
    timestamp: str
    digest: str
    length: int | None
    older: list[tuple[str, str]]
    group: str | None = None
    item: str | None = None
    view: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "key": self.key,
                "url": self.url,
                "kind": self.kind,
                "mime": self.mimetype,
                "ts": self.timestamp,
                "digest": self.digest,
                "length": self.length,
                "older": [list(o) for o in self.older],
                "group": self.group,
                "item": self.item,
                "view": self.view,
            },
            separators=(",", ":"),
        )


@dataclass(slots=True)
class Reduction:
    report: dict[str, Any]
    selected: list[Selected]


def reduce(
    rows: Iterable[cdx.CdxRow],
    scope_filter: ScopeFilter,
    groups: list[UrlGroup],
    *,
    rate_limit_page: str | None = None,
    discovered: Iterable[str] | None = None,
    rate_per_min: float = 10.0,
) -> Reduction:
    rate_rx = re.compile(rate_limit_page, re.I) if rate_limit_page else None
    urls: dict[str, _Url] = {}
    status: Counter[str] = Counter()
    out_of_scope: Counter[str] = Counter()
    rate_digests: set[str] = set()
    total = 0

    for row in rows:
        total += 1
        status[_status_class(row.statuscode)] += 1
        if rate_rx is not None and rate_rx.search(row.original):
            if row.good:
                rate_digests.add(row.digest)
            continue
        kind, reason = scope_filter.classify(row.original, row.mimetype)
        if kind is None:
            out_of_scope[reason] += 1
            continue
        rec = urls.get(row.urlkey)
        if rec is None:
            rec = urls[row.urlkey] = _Url(original=row.original, kind=kind, mimetype=row.mimetype)
        if row.timestamp >= rec.newest_ts:
            rec.newest_ts = row.timestamp
        if not row.good:
            continue
        if row.timestamp >= max((s[0] for s in rec.snaps), default=""):
            # The newest good row decides how the URL is described.
            rec.original, rec.kind, rec.mimetype = row.original, kind, row.mimetype
        item = (row.timestamp, row.digest, row.length)
        if len(rec.snaps) < KEEP_SNAPSHOTS:
            heapq.heappush(rec.snaps, item)
        elif item > rec.snaps[0]:
            heapq.heapreplace(rec.snaps, item)

    # Boilerplate: the rate-limit page's own digests, plus any HTML digest
    # that many different URLs share.
    shared: Counter[str] = Counter()
    for rec in urls.values():
        if rec.kind == "page":
            for digest in {s[1] for s in rec.snaps}:
                shared[digest] += 1
    boilerplate = {d for d, n in shared.items() if n >= SHARED_DIGEST_MIN_URLS}
    bad = rate_digests | boilerplate

    chosen: dict[str, Selected] = {}
    contaminated_urls = 0
    for key, rec in urls.items():
        snaps = sorted(rec.snaps, reverse=True)
        good = [s for s in snaps if s[1] not in bad]
        if snaps and not good:
            contaminated_urls += 1
        if not good:
            continue
        ts, digest, length = good[0]
        chosen[key] = Selected(
            key=key,
            url=rec.original,
            kind=rec.kind,
            mimetype=rec.mimetype,
            timestamp=ts,
            digest=digest,
            length=length,
            older=[(s[0], s[1]) for s in good[1:]],
        )

    selected, group_stats = _apply_groups(chosen, groups)

    pages = [s for s in selected if s.kind == "page"]
    assets = [s for s in selected if s.kind == "asset"]
    report: dict[str, Any] = {
        "version": REPORT_VERSION,
        "rows": total,
        "status": dict(status),
        "out_of_scope": dict(out_of_scope.most_common()),
        "urls_in_scope": len(urls),
        "urls_with_good_snapshot": len(chosen),
        "urls_only_error_pages": contaminated_urls,
        "boilerplate": {
            "rate_limit_digests": sorted(rate_digests),
            "shared_digests": {d: shared[d] for d in sorted(boilerplate)},
        },
        "groups": group_stats,
        "selected": {"total": len(selected), "pages": len(pages), "assets": len(assets)},
        "freshness": {
            "pages": _years(pages),
            "assets": _years(assets),
            "newest": max((s.timestamp for s in selected), default=None),
            "oldest": min((s.timestamp for s in selected), default=None),
        },
        "sections": _sections(selected),
        "estimate": {
            "rate_per_min": rate_per_min,
            "fetches": len(selected),
            "hours": round(len(selected) / max(rate_per_min, 0.1) / 60, 1),
        },
    }
    if discovered is not None:
        report["discovered"] = _compare_discovered(discovered, chosen, selected, groups)
    return Reduction(report=report, selected=selected)


def _sections(items: list[Selected], limit: int = 12) -> list[dict[str, Any]]:
    """The biggest parts of the import, by first path segment.

    The list a user reads to decide what to reject: on fictionmania it showed
    25,066 "report this story" forms that nobody wants archived.
    """
    counts: Counter[tuple[str, str]] = Counter()
    for sel in items:
        if sel.group:
            counts[(f"group: {sel.group}", sel.view or "")] += 1
            continue
        path = urlsplit(sel.url).path or "/"
        first = path.lstrip("/").split("/", 1)[0]
        section = f"/{first}/" if "/" in path.lstrip("/") else f"/{first}"
        counts[(section, sel.kind)] += 1
    return [
        {"section": section, "kind": kind, "count": count}
        for (section, kind), count in counts.most_common(limit)
    ]


def _status_class(code: str) -> str:
    if code and code[0] in "2345" and code.isdigit():
        return f"{code[0]}xx"
    return "revisit" if code == "-" else "other"


def _years(items: list[Selected]) -> dict[str, int]:
    return dict(sorted(Counter(s.timestamp[:4] for s in items).items()))


def _apply_groups(
    chosen: dict[str, Selected], groups: list[UrlGroup]
) -> tuple[list[Selected], list[dict[str, Any]]]:
    if not groups:
        return sorted(chosen.values(), key=_order), []

    members: dict[tuple[int, str], dict[str, list[Selected]]] = defaultdict(
        lambda: defaultdict(list)
    )
    loose: list[Selected] = []
    for sel in chosen.values():
        for index, group in enumerate(groups):
            hit = group.match(sel.url)
            if hit is not None:
                item, view = hit
                sel.group, sel.item, sel.view = group.name, item, view or None
                members[(index, item)][view].append(sel)
                break
        else:
            loose.append(sel)

    picked: list[Selected] = list(loose)
    stats: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        items = {item: views for (i, item), views in members.items() if i == index}
        urls = sum(len(v) for views in items.values() for v in views.values())
        views_seen: Counter[str] = Counter()
        kept = 0
        for views in items.values():
            for view in views:
                views_seen[view or "(any)"] += 1
            choice = _choose(group, views)
            picked.extend(choice)
            kept += len(choice)
        stats.append(
            {
                "name": group.name,
                "items": len(items),
                "urls": urls,
                "fetches": kept,
                "skipped_variants": urls - kept,
                "views": dict(views_seen.most_common()),
            }
        )
    return sorted(picked, key=_order), stats


def _choose(group: UrlGroup, views: dict[str, list[Selected]]) -> list[Selected]:
    """The URLs to fetch for one item: the best preferred view, plus `keep`."""

    def newest(candidates: list[Selected]) -> Selected:
        # The plainest address first, then the newest copy. Wayback holds
        # tracking variants (`&mibextid=` from Facebook shares) of the same
        # story, and replay finds a page by its URL: a story saved only under
        # a variant is missing from the link everyone else uses.
        return max(candidates, key=lambda s: (-_query_params(s.url), s.timestamp))

    if not group.prefer and not group.keep:
        return [newest([s for v in views.values() for s in v])]
    out: list[Selected] = []
    for view in group.prefer:
        if views.get(view):
            out.append(newest(views[view]))
            break
    for view in group.keep:
        if views.get(view) and view not in group.prefer:
            out.append(newest(views[view]))
    if not out:
        # Only views the group does not mention: better one than none.
        out.append(newest([s for v in views.values() for s in v]))
    return out


def _query_params(url: str) -> int:
    query = urlsplit(url).query
    return len([p for p in query.split("&") if p]) if query else 0


def _order(sel: Selected) -> tuple[int, str]:
    """Pages first, then stylesheets, then the rest (docs/17, phase 2)."""
    if sel.kind == "page":
        rank = 0
    elif sel.mimetype == "text/css":
        rank = 1
    elif sel.mimetype.startswith("image/"):
        rank = 2
    else:
        rank = 3
    return rank, sel.key


def _compare_discovered(
    discovered: Iterable[str],
    chosen: dict[str, Selected],
    selected: list[Selected],
    groups: list[UrlGroup],
) -> dict[str, int]:
    """Discovered (sitemap, feeds) against what Wayback can supply."""
    import surt

    covered_items = {(s.group, s.item) for s in selected if s.group}
    seen_keys: set[str] = set()
    found = missing = 0
    for url in discovered:
        try:
            key = surt.surt(url)
        except Exception:  # a malformed discovered URL is just not comparable
            log.debug("discovered URL has no SURT form", extra={"url": url})
            continue
        if key in seen_keys:
            continue
        seen_keys.add(key)
        hit = None
        for group in groups:
            m = group.match(url)
            if m is not None:
                hit = (group.name, m[0])
                break
        if (hit is not None and hit in covered_items) or key in chosen:
            found += 1
        else:
            missing += 1
    return {
        "discovered": len(seen_keys),
        "in_wayback": found,
        "missing_from_wayback": missing,
        "wayback_only_pages": sum(
            1 for s in selected if s.kind == "page" and s.key not in seen_keys
        ),
    }


# ── the whole survey ─────────────────────────────────────────────────────


@dataclass(slots=True)
class Inputs:
    directory: Path
    scope: Scope
    groups: list[UrlGroup]
    rate_limit_page: str | None
    discovered: list[str] | None
    rate_per_min: float


def run(
    inputs: Inputs,
    client: Any,
    *,
    refresh: bool = False,
    say: Callable[[str], None] = lambda _m: None,
    step: Callable[[int, int, str], None] = lambda _d, _t, _m: None,
) -> dict[str, Any]:
    """Pull every crawl host's listing, reduce it, write the results."""
    scope_filter = ScopeFilter(inputs.scope)
    hosts = scope_filter.crawl_hosts
    if not hosts:
        raise SurveyError("the site's scope has no crawlable host to look up")

    cdx_root = inputs.directory / CDX_DIR
    pages_by_host: dict[str, int] = {}
    for host in hosts:
        say(f"Listing what the Wayback Machine holds under {host}/")
        pages_by_host[host] = cdx.pull(
            client, cdx.host_prefix(host), cdx_root / host, refresh=refresh, progress=step
        )

    say("Reducing the listing to one snapshot per URL")

    def rows() -> Iterable[cdx.CdxRow]:
        for host in hosts:
            yield from cdx.iter_rows(cdx_root / host)

    result = reduce(
        rows(),
        scope_filter,
        inputs.groups,
        rate_limit_page=inputs.rate_limit_page,
        discovered=inputs.discovered,
        rate_per_min=inputs.rate_per_min,
    )
    report = {
        **result.report,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "hosts": hosts,
        "cdx_pages": pages_by_host,
        "cdx_requests": getattr(client, "requests", None),
        "groups_config": [g.to_dict() for g in inputs.groups],
        "rate_limit_page": inputs.rate_limit_page,
    }
    write(inputs.directory, report, result.selected)
    return report


def write(directory: Path, report: dict[str, Any], selected: list[Selected]) -> None:
    body = "".join(s.to_json() + "\n" for s in selected).encode("utf-8")
    storage.write_atomic(directory / SELECTION_FILE, gzip.compress(body))
    storage.write_atomic(directory / SURVEY_FILE, json.dumps(report, indent=1))


def read_report(directory: Path) -> dict[str, Any] | None:
    try:
        report = json.loads((directory / SURVEY_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) else None
