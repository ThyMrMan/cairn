"""Reading an archived page instead of replaying it.

Replay is a faithful reconstruction: the page's own CSS, its own JavaScript,
its own fonts, in an iframe on a separate origin. That is the right answer to
*"is this what was published"* and the wrong one to *"I want to read this"*.
It needs pywb running, it needs the subresources to have been captured, and a
page whose stylesheet went missing renders as a column of unstyled text with
none of the affordances a reader view would have given it.

This is the other answer, and it costs nothing to build because M8 already
paid for it: the text is on disk in `derived/text/<capture>.jsonl`, with the
sidebar and navigation already removed by the same extraction that makes
search work. So the reader is a lookup and a render, with no crawler, no pywb
and no JavaScript involved anywhere.

Two things it deliberately is not:

**Not a fallback that hides a broken replay.** It is offered beside replay, not
instead of it, and it says which capture it is reading. A reader view that
silently stood in for a failed replay would make a half-captured site look
fine.

**Not a copy.** Nothing is stored for it. If the extracted text is missing —
a capture from before M8, or one whose text was never rebuilt — the answer is
to say so and point at the button that regenerates it, not to re-parse the
WARC on the fly and pretend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, PageText, Site
from cairn.db.types import to_iso
from cairn.logging import get_logger
from cairn.services import storage, textextract

log = get_logger(__name__)

# Kinds the reader knows how to lay out. Anything else is prose, which is the
# right default for a value written by a newer version of the extractor.
KNOWN_KINDS = frozenset({"h1", "h2", "h3", "li", "quote", "pre", "caption", "p"})


@dataclass(slots=True)
class Block:
    kind: str
    text: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "text": self.text}


@dataclass(slots=True)
class Article:
    url: str
    title: str
    timestamp: str
    capture_dir: str
    capture_id: int | None
    blocks: list[Block] = field(default_factory=list)

    @property
    def words(self) -> int:
        return sum(len(block.text.split()) for block in self.blocks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "timestamp": self.timestamp,
            "capture_dir": self.capture_dir,
            "capture_id": self.capture_id,
            "words": self.words,
            # Roughly 240 words a minute, rounded up. A number nobody should
            # trust to the minute and everybody uses to decide whether to read
            # now or later, which is all it is for.
            "minutes": max(1, round(self.words / 240)) if self.blocks else 0,
            "blocks": [block.to_dict() for block in self.blocks],
        }


@dataclass(slots=True)
class Version:
    """One capture that holds readable text for a URL."""

    capture_id: int | None
    capture_dir: str
    started_at: str | None
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "capture_dir": self.capture_dir,
            "started_at": self.started_at,
            "timestamp": self.timestamp,
        }


def _article_from(page: textextract.Page, *, capture_dir: str, capture_id: int | None) -> Article:
    return Article(
        url=page.url,
        title=page.title,
        timestamp=page.timestamp,
        capture_dir=capture_dir,
        capture_id=capture_id,
        blocks=[
            Block(kind=_kind(page.kind_of(i)), text=text) for i, text in enumerate(page.blocks)
        ],
    )


def _kind(raw: str) -> str:
    return raw if raw in KNOWN_KINDS else "p"


def read(
    session: Session,
    settings: Settings,
    site: Site,
    url: str,
    *,
    capture_dir: str | None = None,
) -> Article | None:
    """One archived page as text, from a named capture or the indexed one.

    Without a capture, the answer comes from the search index, which already
    records the byte range of the page's line — so the common case is a seek
    and a read rather than a scan. With one, the file is scanned, because a
    capture that is not the indexed one has no row pointing into it.
    """
    if capture_dir:
        return _from_capture(session, settings, site, url, capture_dir)

    row = session.scalars(
        select(PageText).where(PageText.site_id == site.id, PageText.url == url).limit(1)
    ).first()
    if row is None:
        return None
    try:
        path = textextract.text_path(settings, site.archive_path, row.capture_dir)
    except storage.StoragePathError:  # pragma: no cover — a hand-edited row
        return None
    page = textextract.read_page_at(path, row.offset, row.length)
    if page is None or page.url != url:
        # The file was rewritten since it was indexed, so the offset points
        # somewhere else. Fall back to a scan rather than showing whatever
        # happened to land at that byte.
        return _from_capture(session, settings, site, url, row.capture_dir)
    return _article_from(page, capture_dir=row.capture_dir, capture_id=row.capture_id)


def _from_capture(
    session: Session, settings: Settings, site: Site, url: str, capture_dir: str
) -> Article | None:
    try:
        path = textextract.text_path(settings, site.archive_path, capture_dir)
    except storage.StoragePathError:
        return None
    if not path.is_file():
        return None
    for page in textextract.read_pages(settings, site.archive_path, capture_dir):
        if page.url == url:
            capture = session.scalars(
                select(Capture)
                .where(Capture.site_id == site.id, Capture.dir_name == capture_dir)
                .limit(1)
            ).first()
            return _article_from(
                page, capture_dir=capture_dir, capture_id=capture.id if capture else None
            )
    return None


def versions(session: Session, settings: Settings, site: Site, url: str) -> list[Version]:
    """Every capture whose extracted text holds this URL, oldest first.

    Read from the text files rather than the replay index on purpose: those
    are the versions this view can actually show, and offering a capture whose
    text was never extracted would be a menu entry that answers "nothing here".
    """
    found: list[Version] = []
    captures = session.scalars(
        select(Capture)
        .where(Capture.site_id == site.id, Capture.status.in_(("ok", "partial")))
        .order_by(Capture.started_at)
    ).all()
    for capture in captures:
        try:
            path = textextract.text_path(settings, site.archive_path, capture.dir_name)
        except storage.StoragePathError:  # pragma: no cover
            continue
        if not path.is_file():
            continue
        for page in textextract.read_pages(settings, site.archive_path, capture.dir_name):
            if page.url != url:
                continue
            found.append(
                Version(
                    capture_id=capture.id,
                    capture_dir=capture.dir_name,
                    started_at=to_iso(capture.started_at) if capture.started_at else None,
                    timestamp=page.timestamp,
                )
            )
            break
    return found


def index_of(session: Session, site: Site, *, limit: int = 200, offset: int = 0) -> dict[str, Any]:
    """Every readable page of a site, newest capture first.

    The way in when you do not already have a URL. Ordered by title rather
    than by URL because this is a reading list, and a list sorted by path puts
    `/2019/01/` above everything anybody wants.
    """
    stmt = (
        select(PageText.url, PageText.title, PageText.words, PageText.timestamp)
        .where(PageText.site_id == site.id)
        .order_by(PageText.timestamp.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = session.execute(stmt).all()
    total = session.scalar(select(func.count(PageText.id)).where(PageText.site_id == site.id)) or 0
    return {
        "pages": [
            {
                "url": row.url,
                "title": row.title or row.url,
                "words": row.words,
                "timestamp": row.timestamp,
            }
            for row in rows
        ],
        "total": total,
    }
