"""Tags: the cross-cutting half of organization.

A site lives in exactly one folder and carries any number of tags. That split
is the whole reason both exist — folders answer "where do I keep this", tags
answer "what is this like", and collapsing either into the other is what makes
a tool need a rewrite two years in.

Tags are global and identified by slug, so `Travel`, `travel` and `TRAVEL` are
one tag with whichever display name was typed first. The slug is also the
directory name under `/data/by-tag`, which is the other reason it has to be
stable: renaming a tag's display text must not move the directory people have
bookmarked on the share.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from cairn.db.models import Site, SiteTag, Tag
from cairn.services import storage

MAX_TAGS_PER_SITE = 50
MAX_NAME_LENGTH = 64
# `#rgb`, `#rrggbb`. Anything else is refused rather than sanitized: a colour
# that silently becomes something else is worse than one that is rejected.
COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class TagError(ValueError):
    """A tag could not be created or changed as requested."""


@dataclass(frozen=True, slots=True)
class TagUsage:
    tag: Tag
    site_count: int


def normalize(name: str) -> tuple[str, str]:
    display = " ".join((name or "").split())[:MAX_NAME_LENGTH]
    if not display:
        raise TagError("a tag needs a name")
    slug = storage.slugify(display, fallback="")
    if not slug:
        raise TagError(f"{name!r} has no letters or digits in it")
    return display, slug


def get_or_create(session: Session, name: str) -> Tag:
    display, slug = normalize(name)
    tag = session.scalar(select(Tag).where(Tag.slug == slug))
    if tag is None:
        tag = Tag(name=display, slug=slug)
        session.add(tag)
        session.flush()
    return tag


def usage(session: Session) -> list[TagUsage]:
    """Every tag with how many live sites carry it.

    An outer join, so a tag with no sites still appears — a tag you just made
    and have not applied yet vanishing from the list would read as the create
    having failed.
    """
    rows = session.execute(
        select(Tag, func.count(Site.id))
        .outerjoin(SiteTag, SiteTag.tag_id == Tag.id)
        .outerjoin(Site, (Site.id == SiteTag.site_id) & Site.deleted_at.is_(None))
        .group_by(Tag.id)
        .order_by(Tag.name)
    ).all()
    return [TagUsage(tag=tag, site_count=int(count)) for tag, count in rows]


def create(session: Session, *, name: str, color: str | None, description: str | None) -> Tag:
    display, slug = normalize(name)
    if session.scalar(select(Tag.id).where(Tag.slug == slug)):
        raise TagError(f"a tag called {display!r} already exists")
    tag = Tag(name=display, slug=slug, color=_color(color), description=description)
    session.add(tag)
    session.flush()
    return tag


def update(
    session: Session,
    tag: Tag,
    *,
    name: str | None = None,
    color: str | None = None,
    description: str | None = None,
) -> Tag:
    """Rename or restyle a tag.

    The slug follows the name, because a tag called `Photography` whose
    directory is `travel` would be a lie on the share. That does move the
    directory, which the caller handles by rebuilding the tag tree.
    """
    if name is not None:
        display, slug = normalize(name)
        clash = session.scalar(select(Tag.id).where(Tag.slug == slug, Tag.id != tag.id))
        if clash:
            raise TagError(f"a tag called {display!r} already exists")
        tag.name = display
        tag.slug = slug
    if color is not None:
        tag.color = _color(color)
    if description is not None:
        tag.description = description or None
    session.flush()
    return tag


def delete_tag(session: Session, tag: Tag) -> int:
    """Remove a tag from every site and delete it. The sites are untouched."""
    removed = (
        session.scalar(select(func.count(SiteTag.site_id)).where(SiteTag.tag_id == tag.id)) or 0
    )
    session.execute(delete(SiteTag).where(SiteTag.tag_id == tag.id))
    session.delete(tag)
    session.flush()
    return int(removed)


# ── bulk ─────────────────────────────────────────────────────────────────


def add_to_sites(session: Session, site_ids: list[int], names: list[str]) -> int:
    """Tag many sites at once, skipping the ones already tagged.

    Reading the existing pairs first rather than relying on the primary key to
    reject duplicates: an IntegrityError inside a batch takes the whole
    transaction down with it, so "tag these 20 sites" would fail entirely
    because one of them already had the tag.
    """
    if not site_ids or not names:
        return 0
    tags = [get_or_create(session, name) for name in names]
    _assert_within_limit(session, site_ids, added=len(tags))

    existing = {
        (site_id, tag_id)
        for site_id, tag_id in session.execute(
            select(SiteTag.site_id, SiteTag.tag_id).where(SiteTag.site_id.in_(site_ids))
        ).all()
    }
    added = 0
    for site_id in site_ids:
        for tag in tags:
            if (site_id, tag.id) not in existing:
                session.add(SiteTag(site_id=site_id, tag_id=tag.id))
                added += 1
    session.flush()
    return added


def remove_from_sites(session: Session, site_ids: list[int], names: list[str]) -> int:
    if not site_ids or not names:
        return 0
    slugs = [normalize(name)[1] for name in names]
    tag_ids = list(session.scalars(select(Tag.id).where(Tag.slug.in_(slugs))).all())
    if not tag_ids:
        return 0
    result = session.execute(
        delete(SiteTag).where(SiteTag.site_id.in_(site_ids), SiteTag.tag_id.in_(tag_ids))
    )
    session.flush()
    return int(getattr(result, "rowcount", 0) or 0)


def _assert_within_limit(session: Session, site_ids: list[int], *, added: int) -> None:
    worst = session.scalar(
        select(func.count(SiteTag.tag_id))
        .where(SiteTag.site_id.in_(site_ids))
        .group_by(SiteTag.site_id)
        .order_by(func.count(SiteTag.tag_id).desc())
        .limit(1)
    )
    if int(worst or 0) + added > MAX_TAGS_PER_SITE:
        raise TagError(f"a site can carry at most {MAX_TAGS_PER_SITE} tags")


def _color(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    color = value.strip()
    if not COLOR.match(color):
        raise TagError(f"{value!r} is not a colour — use #rgb or #rrggbb")
    return color.lower()
