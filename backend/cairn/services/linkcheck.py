"""Which links in the archive lead nowhere when somebody clicks them?

A capture reports what it fetched. It cannot report what the archived pages
*ask for* and the archive does not have, and that gap is the whole experience
of browsing a copy: every dead link is a page that looked archived and was not.

The asset audit answers this for subresources — an image a page references and
the crawl never fetched. This answers it for **navigation**, which needed its
own pass for two reasons. Links are followed by a person rather than by a
renderer, so a broken one is noticed and an absent image often is not. And a
link resolves against the *index* rather than the archive: a record can be in a
WARC and still 404, which is exactly how the pagination pass shipped useless —
69 URLs fetched, 68 of them withheld from the index, and nothing anywhere said
so until somebody clicked Older Posts.

**So the check is against the index keys, not against the captured URLs.** That
is what pywb resolves by, which makes this report what replay will actually do
rather than what the archive theoretically contains. Checking the URL list
instead would have called that broken pass healthy.

**Not every unresolved link is a fault**, and saying so is most of the value.
A link to another site, to a host the domain picker turned off, or to something
the scope deliberately rejects is a boundary working as configured — reporting
those would bury the real misses under thousands of them. What is left is the
narrow, meaningful class: a link to a page on a host this archive crawls, which
the scope did not refuse, and which replay cannot serve.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cairn.logging import get_logger
from cairn.services import htmlrefs, replay, storage

log = get_logger(__name__)

# Enough HTML records to answer the question on a large archive without
# turning a button into a job. Reported when it bites, never silently.
DEFAULT_PAGE_BUDGET = 5_000

# How many linking pages to name per dead target. One is enough to go and look;
# a handful tells you whether it is one stray link or the whole trail.
MAX_SOURCES = 5


@dataclass(slots=True)
class DeadLink:
    target: str
    sources: list[str] = field(default_factory=list)
    # How many pages link to it, which may exceed len(sources).
    link_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"target": self.target, "sources": self.sources, "link_count": self.link_count}


@dataclass(slots=True)
class LinkReport:
    pages_scanned: int = 0
    links_seen: int = 0
    in_scope: int = 0
    resolved: int = 0
    dead: list[DeadLink] = field(default_factory=list)
    truncated: bool = False
    index_records: int = 0

    @property
    def ok(self) -> bool:
        return not self.dead

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "pages_scanned": self.pages_scanned,
            "links_seen": self.links_seen,
            "in_scope": self.in_scope,
            "resolved": self.resolved,
            "dead_count": len(self.dead),
            "dead": [d.to_dict() for d in self.dead],
            "truncated": self.truncated,
            "index_records": self.index_records,
        }


def _index_keys(settings: Any, archive_path: str) -> set[str]:
    """Every URL key replay can answer, read straight off the CDXJ.

    The SURT is taken from the line rather than recomputed from the record's
    URL: it is what a lookup compares against, and rebuilding it here would
    mean this check agreed with itself rather than with pywb.
    """
    path = replay.index_path(settings, archive_path)
    keys: set[str] = set()
    if not path.is_file():
        return keys
    with open(path, "rb") as fh:
        for raw in fh:
            line = raw.decode("utf-8", "replace")
            key, _, rest = line.partition(" ")
            if key and rest:
                keys.add(key)
    return keys


def _html_records(site_root: Path, warcs: list[Path], budget: int) -> Any:
    """Archived HTML, newest capture first, up to a budget.

    Newest first because a dead link in the current archive is the one somebody
    will hit; an older capture's is history. `site_warcs` sorts oldest-first,
    which is right for indexing and backwards for this.
    """
    from warcio.archiveiterator import ArchiveIterator

    seen = 0
    for warc in reversed(warcs):
        if seen >= budget:
            return
        try:
            with open(warc, "rb") as fh:
                for record in ArchiveIterator(fh):
                    if seen >= budget:
                        return
                    if record.rec_type != "response":
                        continue
                    url = record.rec_headers.get_header("WARC-Target-URI") or ""
                    if not url.startswith("http"):
                        continue
                    headers = record.http_headers
                    if headers is None or not str(headers.get_statuscode()).startswith("2"):
                        continue
                    if "html" not in (headers.get_header("Content-Type") or "").lower():
                        continue
                    seen += 1
                    yield url, record.content_stream().read()
        except Exception as exc:  # a truncated WARC is a fact, not a crash
            log.warning(
                "could not read a WARC while checking links",
                extra={"warc": str(warc), "err": str(exc)},
            )


def check_links(
    session: Any,
    settings: Any,
    site: Any,
    *,
    budget: int = DEFAULT_PAGE_BUDGET,
) -> LinkReport:
    """Walk the archived pages and report the links replay cannot answer."""
    from cairn.services import sites as site_service

    report = LinkReport()
    archive_path = site.archive_path
    keys = _index_keys(settings, archive_path)
    report.index_records = len(keys)

    warcs = replay.site_warcs(settings, archive_path)
    if not warcs:
        return report

    scope = site_service.resolved_scope(session, site)
    crawlable = {rule.host.lower() for rule in scope.hosts if rule.crawl_pages}
    # The same list the index withholds by, so a link to something deliberately
    # kept out of replay is not reported as a fault — and, just as importantly,
    # a link the *companion pass* re-admitted is checked rather than excused.
    refused = _compile(replay.withheld_patterns(session, site))

    site_root = storage.site_dir(settings, archive_path)
    dead: dict[str, DeadLink] = {}

    for page_url, body in _html_records(site_root, warcs, budget):
        report.pages_scanned += 1
        try:
            links = htmlrefs.parse_page(body, page_url).links
        except Exception as exc:  # pragma: no cover — malformed markup
            # One unparseable page must not end the check. Logged rather than
            # swallowed, because a report that quietly skipped half the archive
            # would read exactly like a clean one.
            log.warning(
                "could not parse an archived page", extra={"url": page_url, "err": str(exc)}
            )
            continue
        for target in links:
            report.links_seen += 1
            if not _worth_checking(target, crawlable, refused):
                continue
            report.in_scope += 1
            if replay.surt_key(target) in keys:
                report.resolved += 1
                continue
            entry = dead.get(target)
            if entry is None:
                entry = dead[target] = DeadLink(target=target)
            entry.link_count += 1
            if len(entry.sources) < MAX_SOURCES and page_url not in entry.sources:
                entry.sources.append(page_url)

    report.truncated = report.pages_scanned >= budget
    report.dead = sorted(dead.values(), key=lambda d: (-d.link_count, d.target))
    return report


def _compile(patterns: list[str]) -> list[re.Pattern[str]]:
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error:  # a scope somebody typed into; one bad character is not fatal
            continue
    return compiled


def _worth_checking(target: str, crawlable: set[str], refused: list[re.Pattern[str]]) -> bool:
    """Whether an unresolved link would be a fault rather than the boundary.

    Three exclusions, and each removes a class that would otherwise dominate
    the report: another site is not this archive's job; a fragment or a
    `mailto:` is not a page; and something the scope refuses is a decision
    somebody made, not a gap.
    """
    parts = urlsplit(target)
    if parts.scheme not in ("http", "https"):
        return False
    if (parts.hostname or "").lower() not in crawlable:
        return False
    return not any(p.search(target) for p in refused)


def summarize(report: LinkReport) -> str:
    """One line for a log or a job warning."""
    if report.ok:
        return f"every one of {report.in_scope} in-scope link(s) resolves in the index"
    worst = report.dead[0]
    return (
        f"{len(report.dead)} link target(s) do not resolve in replay, from "
        f"{report.pages_scanned} archived page(s) — e.g. {worst.target}"
    )
