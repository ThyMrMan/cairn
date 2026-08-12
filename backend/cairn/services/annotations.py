"""Notes and highlights on archived pages.

docs/13 calls anchoring annotations to replayed content "genuinely hard". It
is not hard here; it is **unavailable**, and for a reason worth stating rather
than working around. Replay runs on a separate origin exactly so archived
JavaScript cannot reach the application — which means the application cannot
reach into the iframe either. No `getSelection`, no injected script, no
coordinates. Any of those would require handing the replay frame our origin,
which is the one thing docs/07 and docs/11 exist to prevent.

So annotations live on the **reader view**: our own origin, our own markup,
text we extracted ourselves. And the anchor is a *quotation* rather than a
position, which turns out to be the more durable choice regardless:

  - Re-extracting a capture rewrites `derived/text/`, and every byte offset
    with it.
  - A later capture of the same page has different offsets again — and the
    interesting case for an annotation is precisely the page that changed.

A quote plus a little context either side survives both. When it genuinely
cannot be found — the paragraph was edited or deleted — the reader says so and
shows the note beside the page rather than silently highlighting whichever
sentence happened to be nearest. An annotation that quietly moves is worse
than one that admits it is lost.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from cairn.db.models import Annotation, Site
from cairn.db.types import to_iso, utcnow
from cairn.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover — typing only
    from cairn.services.reader import Article

log = get_logger(__name__)

MAX_QUOTE_CHARS = 2_000
MAX_NOTE_CHARS = 8_000
# Context kept either side of the quote. Enough to tell two occurrences of a
# short phrase apart; short enough that editing the paragraph around it does
# not lose the anchor.
CONTEXT_CHARS = 40

COLORS = ("yellow", "green", "blue", "pink")

_WS = re.compile(r"\s+")


class AnnotationError(ValueError):
    """The annotation could not be stored as asked."""


@dataclass(slots=True)
class Located:
    """One annotation, placed in the article it was asked about."""

    annotation: Annotation
    block_index: int
    start: int
    end: int
    found: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            **to_dict(self.annotation),
            "block_index": self.block_index,
            "start": self.start,
            "end": self.end,
            "found": self.found,
        }


def to_dict(row: Annotation) -> dict[str, Any]:
    return {
        "id": row.id,
        "site_id": row.site_id,
        "url": row.url,
        "quote": row.quote,
        "note": row.note,
        "color": row.color,
        "created_at": to_iso(row.created_at),
        "updated_at": to_iso(row.updated_at),
    }


def normalize(text: str) -> str:
    """Whitespace as the reader renders it.

    The extractor already collapses runs of whitespace inside a block, but a
    selection made in a browser can carry a newline the DOM inserted for
    layout. Comparing the collapsed forms is what stops a quote failing to
    match the very text it was copied from.
    """
    return _WS.sub(" ", text or "").strip()


def collapse(text: str) -> str:
    """Whitespace collapsed but *not* stripped.

    The right treatment for the context either side of a quote, and the trap
    that broke the first version of this: the space between "mentions" and the
    quotation belongs to the context. Strip it and `before.endswith(prefix)`
    is false for the very text the annotation was made in — so the context
    pass silently never matches, and every ambiguous quote falls through to
    "the first occurrence", which is the failure the context exists to
    prevent.
    """
    return _WS.sub(" ", text or "")


def create(
    session: Session,
    site: Site,
    *,
    url: str,
    quote: str,
    note: str | None = None,
    prefix: str = "",
    suffix: str = "",
    block_index: int = 0,
    color: str = "yellow",
) -> Annotation:
    cleaned = normalize(quote)
    if not cleaned:
        raise AnnotationError("Select some text to annotate.")
    if len(cleaned) > MAX_QUOTE_CHARS:
        raise AnnotationError(
            f"That selection is {len(cleaned)} characters; annotate at most {MAX_QUOTE_CHARS}."
        )
    if note and len(note) > MAX_NOTE_CHARS:
        raise AnnotationError(f"A note can be at most {MAX_NOTE_CHARS} characters.")
    if not (url or "").strip():
        raise AnnotationError("An annotation needs the page it is on.")

    row = Annotation(
        site_id=site.id,
        url=url.strip(),
        quote=cleaned,
        prefix=collapse(prefix)[-CONTEXT_CHARS:],
        suffix=collapse(suffix)[:CONTEXT_CHARS],
        block_index=max(0, block_index),
        note=(note or "").strip() or None,
        color=color if color in COLORS else "yellow",
    )
    session.add(row)
    session.flush()
    log.info("annotation created", extra={"site": site.id, "annotation": row.id})
    return row


def update(
    session: Session, row: Annotation, *, note: str | None = None, color: str | None = None
) -> Annotation:
    if note is not None:
        if len(note) > MAX_NOTE_CHARS:
            raise AnnotationError(f"A note can be at most {MAX_NOTE_CHARS} characters.")
        row.note = note.strip() or None
    if color is not None and color in COLORS:
        row.color = color
    row.updated_at = utcnow()
    session.flush()
    return row


def remove(session: Session, row: Annotation) -> None:
    session.execute(delete(Annotation).where(Annotation.id == row.id))
    session.flush()


def get(session: Session, annotation_id: int) -> Annotation | None:
    return session.get(Annotation, annotation_id)


def for_page(session: Session, site_id: int, url: str) -> list[Annotation]:
    return list(
        session.scalars(
            select(Annotation)
            .where(Annotation.site_id == site_id, Annotation.url == url)
            .order_by(Annotation.block_index, Annotation.id)
        ).all()
    )


def for_site(session: Session, site_id: int, *, limit: int = 200) -> list[Annotation]:
    return list(
        session.scalars(
            select(Annotation)
            .where(Annotation.site_id == site_id)
            .order_by(Annotation.created_at.desc())
            .limit(limit)
        ).all()
    )


def count_for_site(session: Session, site_id: int) -> int:
    return (
        session.scalar(select(func.count(Annotation.id)).where(Annotation.site_id == site_id)) or 0
    )


# ── placing them ─────────────────────────────────────────────────────────


def locate(article: Article, row: Annotation) -> Located:
    """Find an annotation's quote in an article, or report that it is gone.

    Three passes, cheapest first: the block it was made in, then any block
    where the surrounding context also matches, then any block at all. The
    context pass is what keeps a note attached to the right "and then" on a
    page with several — and skipping straight to "any block" would move
    annotations around silently every time a page was edited.
    """
    needle = row.quote
    blocks = [block.text for block in article.blocks]

    if 0 <= row.block_index < len(blocks):
        found = blocks[row.block_index].find(needle)
        if found >= 0:
            return Located(row, row.block_index, found, found + len(needle), True)

    with_context = _find_with_context(blocks, row)
    if with_context is not None:
        index, start = with_context
        return Located(row, index, start, start + len(needle), True)

    for index, text in enumerate(blocks):
        found = text.find(needle)
        if found >= 0:
            return Located(row, index, found, found + len(needle), True)

    return Located(row, -1, 0, 0, False)


def _find_with_context(blocks: list[str], row: Annotation) -> tuple[int, int] | None:
    if not row.prefix and not row.suffix:
        return None
    for index, text in enumerate(blocks):
        start = 0
        while True:
            found = text.find(row.quote, start)
            if found < 0:
                break
            before = text[max(0, found - CONTEXT_CHARS) : found]
            after = text[found + len(row.quote) : found + len(row.quote) + CONTEXT_CHARS]
            if (not row.prefix or before.endswith(row.prefix)) and (
                not row.suffix or after.startswith(row.suffix)
            ):
                return index, found
            start = found + 1
    return None


def resolve(session: Session, site_id: int, article: Article) -> list[dict[str, Any]]:
    """Every annotation on this page, placed where it can be shown."""
    rows = for_page(session, site_id, article.url)
    return [locate(article, row).to_dict() for row in rows]
