"""Site lifecycle: creation, scope persistence, and the on-disk record.

Every mutation that changes something a rebuild would need writes `site.yaml`
before returning. That is the whole basis of "the database is an index over
the filesystem" (docs/03) — if it is written opportunistically, the guarantee
quietly stops being true and nobody finds out until they need it.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Feed, Folder, ScopePattern, ScopeRule, Site, SiteTag, Tag
from cairn.db.types import utcnow
from cairn.logging import get_logger
from cairn.services import storage
from cairn.services import tags as tag_service
from cairn.services.filters import SiteFilter
from cairn.services.scope import HostRule, Scope, ScopeError, default_scope

log = get_logger(__name__)

MAX_SITES_PER_FOLDER = 10_000


class SiteError(ValueError):
    """A site could not be created or updated as requested."""


def normalize_seed_url(raw: str) -> str:
    """Canonicalize a seed URL, or explain why it is unusable.

    Users paste `example.com`, `Example.COM/`, and full URLs with fragments.
    All three should work; a bare hostname becomes https, since a site that
    only speaks http will redirect and wget follows it.
    """
    candidate = (raw or "").strip()
    if not candidate:
        raise SiteError("Enter the address of the site you want to archive.")
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https"):
        raise SiteError(f"{parts.scheme}:// is not supported — use http or https.")
    if not parts.hostname:
        raise SiteError(f"{raw!r} does not contain a hostname.")
    if "." not in parts.hostname and parts.hostname != "localhost":
        raise SiteError(f"{parts.hostname!r} does not look like a hostname.")

    netloc = parts.hostname.lower()
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    # Drop the fragment: it never reaches the server, and keeping it would
    # make two seeds for the same page look like different sites.
    return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))


def suggest_title(seed_url: str) -> str:
    host = urlsplit(seed_url).hostname or seed_url
    return host


# ── creation ─────────────────────────────────────────────────────────────


def create_site(
    session: Session,
    settings: Settings,
    *,
    seed_url: str,
    title: str | None = None,
    folder_id: int | None = None,
    notes: str | None = None,
    engine_id: str = "wget-warc",
    engine_config: dict[str, Any] | None = None,
    profile_id: int | None = None,
    keep_mirror: bool = False,
    tags: list[str] | None = None,
) -> Site:
    seed = normalize_seed_url(seed_url)
    host = urlsplit(seed).hostname or ""

    folder = _resolve_folder(session, folder_id)
    taken = set(session.scalars(select(Site.slug).where(Site.folder_id == folder.id)).all())
    slug = storage.unique_slug(storage.slugify(title or host), taken)
    archive_path = f"{folder.path}/{slug}" if folder.path else slug

    site = Site(
        folder_id=folder.id,
        slug=slug,
        title=(title or suggest_title(seed)).strip()[:255],
        seed_url=seed,
        primary_host=host,
        notes=notes,
        profile_id=profile_id,
        engine_id=engine_id,
        engine_config=engine_config or {},
        keep_mirror=keep_mirror,
        status="new",
        archive_path=archive_path,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(site)
    session.flush()

    save_scope(session, site, default_scope(seed))
    if tags:
        from cairn.services import symlinks

        set_tags(session, site, tags)
        symlinks.safe_rebuild(session, settings)

    storage.ensure_site_dirs(settings, site.archive_path)
    session.flush()
    write_site_yaml(session, settings, site)

    log.info("site created", extra={"site": site.id, "slug": slug, "host": host})
    return site


def _resolve_folder(session: Session, folder_id: int | None) -> Folder:
    from cairn.services import folders

    try:
        if folder_id is not None:
            return folders.require_folder(session, folder_id)
        return folders.root_folder(session)
    except folders.FolderError as exc:
        raise SiteError(str(exc)) from exc


# ── scope ────────────────────────────────────────────────────────────────


def load_scope(session: Session, site: Site) -> Scope:
    """Reassemble the resolved scope from its normalized rows.

    This is what the engine receives, so it must be complete: per-host rules
    from `scope_rules`, patterns from `scope_patterns`, and everything
    site-level from `sites.scope_settings`.
    """
    rules = session.scalars(
        select(ScopeRule).where(ScopeRule.site_id == site.id).order_by(ScopeRule.id)
    ).all()
    if not rules:
        return default_scope(site.seed_url)

    patterns = session.scalars(
        select(ScopePattern).where(ScopePattern.site_id == site.id).order_by(ScopePattern.id)
    ).all()

    scope = Scope(
        seeds=[site.seed_url],
        hosts=[
            HostRule(
                host=rule.host,
                crawl_pages=rule.crawl_pages,
                fetch_assets=rule.fetch_assets,
                path_prefix=rule.path_prefix,
                allow_extensionless=rule.allow_extensionless,
            )
            for rule in rules
        ],
        accept_patterns=[p.pattern for p in patterns if p.kind == "accept"],
        reject_patterns=[p.pattern for p in patterns if p.kind == "reject"],
    )

    stored: dict[str, Any] = site.scope_settings or {}
    scope.max_bytes = stored.get("max_bytes")
    scope.max_pages = stored.get("max_pages")
    scope.max_depth = stored.get("max_depth")
    scope.obey_robots = bool(stored.get("obey_robots", True))
    scope.exclude_hosts = list(stored.get("exclude_hosts") or [])
    scope.seed_urls_from = dict(stored.get("seed_urls_from") or scope.seed_urls_from)
    scope.path_prefix = stored.get("path_prefix")
    scope.politeness.update(stored.get("politeness") or {})
    return scope


def save_scope(session: Session, site: Site, scope: Scope) -> None:
    """Replace a site's scope wholesale.

    Rewriting rather than diffing: a scope is small, it is always submitted in
    full by the domain picker, and a partial update that leaves a stale host
    rule behind is a crawl that wanders somewhere the user thought they had
    deselected.
    """
    scope.validate()

    session.query(ScopeRule).filter(ScopeRule.site_id == site.id).delete()
    session.query(ScopePattern).filter(ScopePattern.site_id == site.id).delete()

    for rule in scope.hosts:
        session.add(
            ScopeRule(
                site_id=site.id,
                host=rule.host,
                crawl_pages=rule.crawl_pages,
                fetch_assets=rule.fetch_assets,
                path_prefix=rule.path_prefix,
                allow_extensionless=rule.allow_extensionless,
            )
        )
    for kind, patterns in (("accept", scope.accept_patterns), ("reject", scope.reject_patterns)):
        for pattern in patterns:
            session.add(ScopePattern(site_id=site.id, kind=kind, pattern=pattern))

    site.scope_settings = {
        "max_bytes": scope.max_bytes,
        "max_pages": scope.max_pages,
        "max_depth": scope.max_depth,
        "obey_robots": scope.obey_robots,
        "exclude_hosts": scope.exclude_hosts,
        "seed_urls_from": scope.seed_urls_from,
        "path_prefix": scope.path_prefix,
        "politeness": scope.politeness,
    }
    site.updated_at = utcnow()
    session.flush()


def resolved_scope(session: Session, site: Site) -> Scope:
    """Scope as the engine will see it."""
    scope = load_scope(session, site)
    scope.seeds = [site.seed_url]
    return scope


def scope_is_unindexed(session: Session, site: Site) -> bool:
    """Whether the site still has the pre-discovery fallback scope.

    `load_scope` falls back to `default_scope` — the seed host and nothing
    else — when no rules have been saved. That is the right default, but a
    capture that runs on it archives the HTML and none of the images, CSS or
    JS the pages pull from anywhere else, and nothing about the result says
    why. Callers use this to say so out loud.
    """
    count = session.scalar(select(func.count(ScopeRule.id)).where(ScopeRule.site_id == site.id))
    return not count


# ── tags ─────────────────────────────────────────────────────────────────


def set_tags(session: Session, site: Site, names: list[str]) -> list[Tag]:
    """Replace a site's tags wholesale.

    Naming goes through `tags.get_or_create`, so the slug rules — and the
    directory names under `/data/by-tag` that follow from them — are decided in
    one place rather than here as well.
    """
    wanted = [n.strip() for n in names if n and n.strip()]
    if len(wanted) > tag_service.MAX_TAGS_PER_SITE:
        raise SiteError(f"a site can carry at most {tag_service.MAX_TAGS_PER_SITE} tags")

    session.query(SiteTag).filter(SiteTag.site_id == site.id).delete()
    tags: list[Tag] = []
    for name in dict.fromkeys(wanted):  # de-duplicate, preserve order
        try:
            tag = tag_service.get_or_create(session, name)
        except tag_service.TagError as exc:
            raise SiteError(str(exc)) from exc
        if tag.id in {t.id for t in tags}:  # two names, one slug
            continue
        session.add(SiteTag(site_id=site.id, tag_id=tag.id))
        tags.append(tag)
    session.flush()
    return tags


def tags_for(session: Session, site_id: int) -> list[str]:
    return list(
        session.scalars(
            select(Tag.name)
            .join(SiteTag, SiteTag.tag_id == Tag.id)
            .where(SiteTag.site_id == site_id)
            .order_by(Tag.name)
        ).all()
    )


def tag_map(session: Session, site_ids: list[int]) -> dict[int, list[str]]:
    """Tags for many sites in one query.

    The list endpoint needs these for every row, and doing it per site is the
    N+1 that makes a site list slow exactly when somebody has enough sites to
    want one.
    """
    if not site_ids:
        return {}
    rows = session.execute(
        select(SiteTag.site_id, Tag.name)
        .join(Tag, Tag.id == SiteTag.tag_id)
        .where(SiteTag.site_id.in_(site_ids))
        .order_by(SiteTag.site_id, Tag.name)
    ).all()
    out: dict[int, list[str]] = {site_id: [] for site_id in site_ids}
    for site_id, name in rows:
        out.setdefault(site_id, []).append(name)
    return out


# ── the on-disk record ───────────────────────────────────────────────────


def write_site_yaml(session: Session, settings: Settings, site: Site) -> None:
    scope = resolved_scope(session, site)
    folder = session.get(Folder, site.folder_id)
    profile_name = site.profile.name if site.profile is not None else None
    feeds = session.scalars(select(Feed).where(Feed.site_id == site.id)).all()

    payload = storage.build_site_yaml(
        site_id=site.id,
        slug=site.slug,
        title=site.title,
        seed_url=site.seed_url,
        primary_host=site.primary_host,
        folder=folder.path if folder else "",
        tags=tags_for(session, site.id),
        engine_id=site.engine_id,
        access_profile=profile_name,
        scope=scope.to_dict(),
        feeds=[{"url": f.url, "interval_min": f.interval_min} for f in feeds],
        created_at=site.created_at,
    )
    try:
        storage.write_yaml(storage.site_yaml_path(settings, site.archive_path), payload)
    except OSError as exc:
        # Losing site.yaml costs rebuildability, not the running system. Log
        # loudly and continue rather than failing the user's request.
        log.error("could not write site.yaml", extra={"site": site.id, "err": str(exc)})


# ── queries ──────────────────────────────────────────────────────────────


def visible() -> Select[tuple[Site]]:
    return select(Site).where(Site.deleted_at.is_(None))


def list_sites(
    session: Session,
    site_filter: SiteFilter,
    *,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[Site], int]:
    """Filtered, sorted, paged. Every condition comes from the one filter
    object, so the API and a saved view cannot mean different things by the
    same query (docs/09)."""
    stmt = site_filter.apply(session, visible())
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    stmt = site_filter.order(stmt).limit(per_page).offset((page - 1) * per_page)
    return list(session.scalars(stmt).all()), total


def get_site(session: Session, site_id: int) -> Site | None:
    site = session.get(Site, site_id)
    if site is None or site.deleted_at is not None:
        return None
    return site


def raise_if_scope_invalid(scope: Scope) -> None:
    try:
        scope.validate()
    except ScopeError as exc:
        raise SiteError(str(exc)) from exc
