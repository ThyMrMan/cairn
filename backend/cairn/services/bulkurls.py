"""Turning a pile of URLs into sites.

For a migration, or a one-off collection: paste a list, get archives. The
work is almost entirely in deciding what a list of URLs *means*, and there are
three answers that look obvious and are wrong.

**A pasted URL is a page, not a site.** Seeding a site at
`blog.example.com/2019/03/some-post.html` gives an archive whose identity is
one post and whose scope is derived from it. So a group's site is seeded at
the *origin*, and the pasted URLs become the capture's seeds.

**And therefore the capture must not crawl.** Fifty bookmarks across fifty
domains, each triggering a full crawl of somebody's site, is a plausible way
to get an IP address blocked and a certain way to fill a disk. The default is
`only_extra_seeds` — archive exactly the pages that were listed and nothing
else — with crawling available per group and never assumed.

**Grouping by registrable domain means a group can span hosts.** `example.com`
and `www.example.com` are one site and two hosts; a scope built from the first
URL's host silently rejects everything on the other. Every host in the group
goes into the scope.

The parser reads *any* text and takes every http(s) URL out of it, which means
a Netscape `bookmarks.html`, a markdown list, a CSV column and a plain list of
lines all work without a format selector — and without a parser per format
that would each have their own way of being subtly wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Site
from cairn.discovery import hosts as host_classify
from cairn.logging import get_logger
from cairn.services import sites as site_service
from cairn.services.scope import HostRule

log = get_logger(__name__)

# A paste is a person's clipboard, not a data feed. Enough for a bookmark
# export; small enough that the survey is instant and no single import can
# queue a thousand crawls.
MAX_URLS = 5_000
MAX_INPUT_BYTES = 4 * 1024 * 1024

# Deliberately greedy about what counts as a URL and strict about where one
# ends: a trailing `)`, `"`, `<` or `,` belongs to the markdown, HTML or CSV
# around it, never to the address.
_URL = re.compile(r"""https?://[^\s"'<>)\]]+""", re.IGNORECASE)
_TRAILING = ".,;:!?"


class BulkImportError(ValueError):
    """The input could not be turned into a list of URLs."""


@dataclass(slots=True)
class Group:
    """One site's worth of pasted URLs."""

    key: str
    origin: str
    hosts: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    site_id: int | None = None
    site_title: str | None = None

    @property
    def is_new(self) -> bool:
        return self.site_id is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "origin": self.origin,
            "hosts": self.hosts,
            "urls": self.urls[:20],
            "url_count": len(self.urls),
            "site_id": self.site_id,
            "site_title": self.site_title,
            "is_new": self.is_new,
        }


@dataclass(slots=True)
class Survey:
    groups: list[Group] = field(default_factory=list)
    found: int = 0
    skipped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "groups": [g.to_dict() for g in self.groups],
            "new_sites": sum(1 for g in self.groups if g.is_new),
            "existing_sites": sum(1 for g in self.groups if not g.is_new),
            "skipped": self.skipped[:20],
            "skipped_count": len(self.skipped),
        }


@dataclass(slots=True)
class Result:
    created: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    jobs: list[int] = field(default_factory=list)
    urls: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "jobs": self.jobs,
            "urls": self.urls,
            "errors": self.errors,
        }


def extract(text: str) -> tuple[list[str], list[str]]:
    """Every http(s) URL in whatever was pasted, plus what was thrown away.

    Order is preserved and duplicates are dropped, because a bookmark export
    lists the same page under two folders and a person expects to get one
    archive out of that, not two.
    """
    if len(text.encode("utf-8", "ignore")) > MAX_INPUT_BYTES:
        raise BulkImportError("That is more than 4 MB of text; paste a smaller list.")

    seen: dict[str, None] = {}
    skipped: list[str] = []
    for raw in _URL.finditer(text or ""):
        candidate = raw.group(0).rstrip(_TRAILING)
        try:
            normalized = _normalize(candidate)
        except ValueError as exc:
            skipped.append(f"{candidate[:120]}: {exc}")
            continue
        if len(seen) >= MAX_URLS:
            skipped.append(f"stopped at {MAX_URLS} URLs; the rest of the list was ignored")
            break
        seen.setdefault(normalized, None)
    return list(seen), skipped


def _normalize(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("not an http(s) URL")
    netloc = parts.hostname.lower()
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    # The fragment never reaches the server, so two bookmarks of one page that
    # differ only after the `#` are one page.
    return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))


def survey(session: Session, text: str) -> Survey:
    """What importing this list would do, before it does any of it."""
    urls, skipped = extract(text)
    result = Survey(found=len(urls), skipped=skipped)

    grouped: dict[str, Group] = {}
    for url in urls:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        key = host_classify.registrable_domain(host)
        group = grouped.get(key)
        if group is None:
            group = Group(key=key, origin=f"{parts.scheme}://{parts.netloc}")
            grouped[key] = group
        if host not in group.hosts:
            group.hosts.append(host)
        group.urls.append(url)

    for group in grouped.values():
        existing = _existing_site(session, group)
        if existing is not None:
            group.site_id = existing.id
            group.site_title = existing.title

    result.groups = sorted(grouped.values(), key=lambda g: (-len(g.urls), g.key))
    return result


def _existing_site(session: Session, group: Group) -> Site | None:
    """A site already archiving this domain, if there is one.

    Matched on `primary_host` and on the group's hosts rather than on the
    exact URL: the point is not to end up with two sites for one blog because
    a bookmark pointed at `www.` and the site was added without it.
    """
    rows = session.scalars(site_service.visible()).all()
    for site in rows:
        host = (site.primary_host or "").lower()
        if host in group.hosts:
            return site
        if host and host_classify.registrable_domain(host) == group.key:
            return site
    return None


def import_urls(
    session: Session,
    settings: Settings,
    text: str,
    *,
    folder_id: int | None = None,
    tags: list[str] | None = None,
    supervisor: Any = None,
    capture: bool = True,
    crawl: bool = False,
) -> Result:
    """Create or reuse a site per domain and queue a capture of the listed pages.

    `crawl=False` — the default — archives exactly the URLs that were pasted.
    A list of fifty bookmarks is fifty pages, and turning it into fifty full
    crawls of fifty strangers' sites is the sort of thing that gets an IP
    address blocked.
    """
    plan = survey(session, text)
    result = Result(urls=plan.found)

    for group in plan.groups:
        try:
            site = _site_for(session, settings, group, folder_id=folder_id, tags=tags)
        except site_service.SiteError as exc:
            # One unusable domain must not abandon the import, for the reason
            # the ArchiveBox importer learned: a real list accumulated over
            # years has entries nobody remembers adding.
            result.errors.append(f"{group.key}: {exc}")
            continue

        if group.site_id is None:
            result.created.append(site.id)
        else:
            result.updated.append(site.id)

        if capture and supervisor is not None:
            job = supervisor.enqueue(
                session,
                job_type="capture",
                site_id=site.id,
                spec={
                    "kind": "incremental" if not crawl else "full",
                    "extra_seeds": group.urls,
                    # The whole safety property of this feature.
                    "only_extra_seeds": not crawl,
                },
                priority=150,
            )
            result.jobs.append(job.id)

    session.flush()
    return result


def _site_for(
    session: Session,
    settings: Settings,
    group: Group,
    *,
    folder_id: int | None,
    tags: list[str] | None,
) -> Site:
    if group.site_id is not None:
        site = session.get(Site, group.site_id)
        if site is not None:
            _widen_scope(session, settings, site, group)
            return site

    site = site_service.create_site(
        session,
        settings,
        # The origin, not the first bookmark. A site whose seed is one post is
        # a site whose every future capture starts from that post.
        seed_url=group.origin,
        folder_id=folder_id,
        tags=tags,
    )
    _widen_scope(session, settings, site, group)
    return site


def _widen_scope(session: Session, settings: Settings, site: Site, group: Group) -> None:
    """Make sure every host in the group is inside the site's scope.

    Grouping by registrable domain is what makes `example.com` and
    `www.example.com` one site — and what makes a scope built from one of them
    reject the pages on the other, which reads as a capture that archived half
    the list for no stated reason.
    """
    scope = site_service.load_scope(session, site)
    known = {rule.host for rule in scope.hosts}
    added = [host for host in group.hosts if host not in known]
    if not added:
        return
    for host in added:
        scope.hosts.append(HostRule(host=host, crawl_pages=True, fetch_assets=True))
    site_service.save_scope(session, site, scope)
    site_service.write_site_yaml(session, settings, site)
    log.info("bulk import widened a scope", extra={"site": site.id, "hosts": len(added)})
