"""One filter object, used by the filter bar and by saved views.

docs/09 puts it as plainly as it can be put: *the same filter object
serializes into `saved_views.query`, so a saved smart view is literally a
stored query string. Keep them identical — a divergence between "what the
filter bar produces" and "what a saved view stores" is a bug factory.*

So there is exactly one class here and it can do four things: read itself out
of query parameters, write itself back to query parameters, read itself out of
stored JSON, and compile itself to SQL. Every path in and out of a filter goes
through it, which turns "do these two agree?" from a thing to remember into a
round-trip test.

Unset is `None`, and only set fields are serialized. That is what makes a
saved view survive new filter fields being added later: a view saved today
carries no opinion about a field that did not exist, rather than an accidental
`false` that would silently narrow it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from cairn.db.models import Capture, Folder, Site, SiteTag, Tag

# The character `like_contains` escapes with. Passed to every `ilike` that uses
# it; SQLite and Postgres both take an explicit ESCAPE clause.
LIKE_ESCAPE = "\\"


def like_contains(value: str) -> str:
    """A LIKE pattern matching rows that *contain* `value` literally.

    `%` and `_` are wildcards in SQL LIKE, and both turn up in ordinary input:
    `100%` is a thing somebody types, `my_site` is a plausible slug, and a
    URL-encoded search term is mostly percent signs. Unescaped, `%` matched
    every row in the table rather than the rows containing a percent sign —
    measured against the capture URL list, where searching for `%` returned
    the entire archive.

    Callers must pass `escape=LIKE_ESCAPE` alongside the pattern, or the
    backslashes this adds are matched literally instead of escaping.
    """
    escaped = (
        value.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", f"{LIKE_ESCAPE}%")
        .replace("_", f"{LIKE_ESCAPE}_")
    )
    return f"%{escaped}%"


SORT_COLUMNS = {
    "title": Site.title,
    "created_at": Site.created_at,
    "updated_at": Site.updated_at,
    "last_capture_at": Site.last_capture_at,
    "size_bytes": Site.size_bytes,
    "url_count": Site.url_count,
}
DEFAULT_SORT = "-updated_at"
TAG_MODES = ("all", "any")
MAX_TAGS = 20


class FilterError(ValueError):
    """A filter could not be understood."""


@dataclass(slots=True)
class SiteFilter:
    folder_id: int | None = None
    folder_recursive: bool = True
    tags: list[str] = field(default_factory=list)
    tag_mode: str = "all"
    status: str | None = None
    engine_id: str | None = None
    profile_id: int | None = None
    host: str | None = None
    has_errors: bool | None = None
    never_captured: bool | None = None
    last_capture_after: str | None = None
    last_capture_before: str | None = None
    size_min: int | None = None
    size_max: int | None = None
    q: str | None = None
    sort: str = DEFAULT_SORT

    # ── in ───────────────────────────────────────────────────────────────

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> SiteFilter:
        """Build from query parameters.

        `tag` accepts a repeated parameter or a comma-separated one — both
        forms reach a server from real clients, and rejecting either would be
        a distinction nobody can see from the outside.
        """
        raw = dict(params)
        instance = cls()

        instance.folder_id = _int(raw.get("folder_id"), "folder_id")
        instance.folder_recursive = _bool(raw.get("folder_recursive"), default=True)
        instance.tags = _tags(raw.get("tag") or raw.get("tags"))
        mode = str(raw.get("tag_mode") or "all").lower()
        if mode not in TAG_MODES:
            raise FilterError(f"tag_mode must be one of {', '.join(TAG_MODES)}")
        instance.tag_mode = mode

        instance.status = _text(raw.get("status"))
        instance.engine_id = _text(raw.get("engine_id"))
        instance.profile_id = _int(raw.get("profile_id"), "profile_id")
        instance.host = _text(raw.get("host"))
        instance.has_errors = _tribool(raw.get("has_errors"))
        instance.never_captured = _tribool(raw.get("never_captured"))
        instance.last_capture_after = _timestamp(raw.get("last_capture_after"))
        instance.last_capture_before = _timestamp(raw.get("last_capture_before"))
        instance.size_min = _int(raw.get("size_min"), "size_min")
        instance.size_max = _int(raw.get("size_max"), "size_max")
        instance.q = _text(raw.get("q"))

        sort = str(raw.get("sort") or DEFAULT_SORT)
        if sort.lstrip("-") not in SORT_COLUMNS:
            raise FilterError(f"cannot sort by {sort!r}")
        instance.sort = sort
        return instance

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> SiteFilter:
        """Build from stored JSON — the same reader, so they cannot drift."""
        return cls.from_params(data or {})

    # ── out ──────────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Only what differs from the defaults."""
        blank = SiteFilter()
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if value != getattr(blank, f.name):
                out[f.name] = value
        return out

    def to_query_string(self) -> str:
        pairs: list[tuple[str, str]] = []
        for key, value in self.to_dict().items():
            if key == "tags":
                pairs.extend(("tag", tag) for tag in value)
            elif isinstance(value, bool):
                pairs.append((key, "true" if value else "false"))
            else:
                pairs.append((key, str(value)))
        return urlencode(pairs)

    @property
    def is_empty(self) -> bool:
        return not self.to_dict()

    # ── to SQL ───────────────────────────────────────────────────────────

    def apply(self, session: Session, stmt: Select[tuple[Site]]) -> Select[tuple[Site]]:
        if self.folder_id is not None:
            stmt = stmt.where(Site.folder_id.in_(self._folder_ids(session)))
        if self.tags:
            stmt = self._apply_tags(stmt)
        if self.status:
            stmt = stmt.where(Site.status == self.status)
        if self.engine_id:
            stmt = stmt.where(Site.engine_id == self.engine_id)
        if self.profile_id is not None:
            stmt = stmt.where(Site.profile_id == self.profile_id)
        if self.host:
            stmt = stmt.where(Site.primary_host.ilike(like_contains(self.host), escape=LIKE_ESCAPE))
        if self.never_captured is not None:
            stmt = stmt.where(
                Site.last_capture_at.is_(None)
                if self.never_captured
                else Site.last_capture_at.isnot(None)
            )
        if self.has_errors is not None:
            faulty = select(Capture.site_id).where(Capture.error_count > 0)
            stmt = stmt.where(Site.id.in_(faulty) if self.has_errors else Site.id.not_in(faulty))
        if self.last_capture_after:
            stmt = stmt.where(Site.last_capture_at >= _parse(self.last_capture_after))
        if self.last_capture_before:
            stmt = stmt.where(Site.last_capture_at <= _parse(self.last_capture_before))
        if self.size_min is not None:
            stmt = stmt.where(Site.size_bytes >= self.size_min)
        if self.size_max is not None:
            stmt = stmt.where(Site.size_bytes <= self.size_max)
        if self.q:
            needle = like_contains(self.q.strip())
            stmt = stmt.where(
                or_(
                    Site.title.ilike(needle, escape=LIKE_ESCAPE),
                    Site.seed_url.ilike(needle, escape=LIKE_ESCAPE),
                    Site.primary_host.ilike(needle, escape=LIKE_ESCAPE),
                    Site.notes.ilike(needle, escape=LIKE_ESCAPE),
                )
            )
        return stmt

    def order(self, stmt: Select[tuple[Site]]) -> Select[tuple[Site]]:
        column = SORT_COLUMNS.get(self.sort.lstrip("-"), Site.updated_at)
        ordered = column.desc() if self.sort.startswith("-") else column.asc()
        # A stable tiebreak, so paging through sites that share a timestamp
        # cannot show one twice and skip another.
        return stmt.order_by(ordered, Site.id.asc())

    def _folder_ids(self, session: Session) -> list[int]:
        folder = session.get(Folder, self.folder_id)
        if folder is None:
            return [-1]  # a folder that does not exist matches nothing
        if not self.folder_recursive:
            return [folder.id]
        below = session.scalars(
            select(Folder.id).where(Folder.path.startswith(f"{folder.path}/", autoescape=True))
        ).all()
        return [folder.id, *below]

    def _apply_tags(self, stmt: Select[tuple[Site]]) -> Select[tuple[Site]]:
        tagged = select(SiteTag.site_id).join(Tag, Tag.id == SiteTag.tag_id)
        if self.tag_mode == "any":
            return stmt.where(Site.id.in_(tagged.where(Tag.slug.in_(self.tags))))
        # "all" is an intersection, and doing it as one subquery per tag keeps
        # it readable and lets SQLite use the same index each time. The tag
        # count is capped, so this cannot grow into a query nobody can plan.
        for tag in self.tags:
            stmt = stmt.where(Site.id.in_(tagged.where(Tag.slug == tag)))
        return stmt


# ── parsing ──────────────────────────────────────────────────────────────


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any, name: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise FilterError(f"{name} must be a number") from exc


def _bool(value: Any, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _tribool(value: Any) -> bool | None:
    """Three-state: unset, true, false — `has_errors=false` is a real filter."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _tags(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    raw: Sequence[Any] = value if isinstance(value, (list, tuple)) else str(value).split(",")
    seen: list[str] = []
    for item in raw:
        slug = str(item).strip().lower()
        if slug and slug not in seen:
            seen.append(slug)
    if len(seen) > MAX_TAGS:
        raise FilterError(f"at most {MAX_TAGS} tags can be combined")
    return seen


def _timestamp(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    _parse(text)  # reject now, with a message, rather than at query time
    return text


def _parse(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FilterError(f"{value!r} is not a date I can read (try 2026-08-01)") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
