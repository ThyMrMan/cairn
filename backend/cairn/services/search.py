"""Full-text search across every archive.

The index is SQLite FTS5, contentless: it holds the terms and no copy of the
documents. The text stays where the extractor put it, in
`derived/text/<capture>.jsonl` beside the archive, and a result's snippet is a
seek into that file. See the migration for why — briefly, the database is
copied before every migration and ten backups are kept, so a second copy of
every archived page would be paid for eleven times over.

Two things follow from contentless FTS5, both measured against SQLite 3.46.1:

  * `snippet()` returns NULL, so snippets are built here.
  * an UPDATE of a subset of columns is rejected outright, so a page that is
    captured again is deleted from the index and re-inserted.

**The query box is not FTS5 syntax and must not be treated as it.** Typing
`c++` or a bare quote is a syntax error inside MATCH, and `AND` is an operator
rather than a word; a search box that raises "fts5: syntax error near" is a
search box people stop using. Every bare term is therefore quoted into a
string literal, and the two operators worth having — `"a phrase"` and a
trailing `*` — are recognised deliberately rather than passed through.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, PageText, Site, SiteTag, Tag
from cairn.db.types import utcnow
from cairn.logging import get_logger
from cairn.services import storage, textextract

log = get_logger(__name__)

FTS_TABLE = "page_text_fts"
SNIPPET_RADIUS = 90
MAX_SNIPPETS = 3
DEFAULT_LIMIT = 25
MAX_LIMIT = 100
# Enough to rank well without reading a whole site's worth of rows for a
# common word. bm25 orders inside this window, so a term in every page still
# returns its best matches rather than its first ones.
CANDIDATE_LIMIT = 2000

INDEX_SETTING = "search.index_captures"

# A term ends at whitespace; a phrase is anything inside double quotes.
_TOKENS = re.compile(r'"([^"]*)"|(\S+)')
# FTS5 string literals escape a double quote by doubling it. Nothing else
# needs escaping inside one, which is the whole reason for quoting.
_UNSAFE = re.compile(r'["\x00]')


class SearchError(RuntimeError):
    """The query could not be run."""


@dataclass(slots=True)
class Hit:
    site_id: int
    site_title: str
    site_slug: str
    folder_path: str
    url: str
    title: str
    snippets: list[str]
    score: float
    capture_id: int | None
    timestamp: str
    words: int


@dataclass(slots=True)
class Results:
    hits: list[Hit]
    total: int
    query: str
    terms: list[str] = field(default_factory=list)
    truncated: bool = False


# ── turning a search box into a MATCH expression ─────────────────────────


@dataclass(slots=True)
class ParsedQuery:
    expression: str
    #: What to look for in the stored text when building a snippet. Phrases
    #: stay whole; a prefix term keeps only its stem.
    terms: list[str]
    negated: list[str]

    @property
    def empty(self) -> bool:
        return not self.expression


def parse_query(raw: str) -> ParsedQuery:
    """Translate what somebody typed into something FTS5 will accept.

    Supported deliberately: `"exact phrase"`, a trailing `*` for prefix, and a
    leading `-` to exclude. Everything else — including `AND`, `OR`, `NEAR`,
    colons and punctuation — is a word to search for, because that is what a
    person typing it means.
    """
    parts: list[str] = []
    terms: list[str] = []
    negated: list[str] = []

    for match in _TOKENS.finditer(raw or ""):
        phrase, word = match.group(1), match.group(2)
        if phrase is not None:
            cleaned = " ".join(phrase.split())
            if not cleaned:
                continue
            parts.append(_literal(cleaned))
            terms.append(cleaned)
            continue

        token = word or ""
        exclude = token.startswith("-") and len(token) > 1
        if exclude:
            token = token[1:]
        prefix = token.endswith("*") and len(token) > 1
        if prefix:
            token = token[:-1]
        token = token.strip()
        if not token:
            continue

        expr = _literal(token) + ("*" if prefix else "")
        if exclude:
            parts.append(f"NOT {expr}")
            negated.append(token)
        else:
            parts.append(expr)
            terms.append(token)

    # `NOT` cannot open an FTS5 expression, and a query that is only
    # exclusions has nothing to rank anyway.
    if parts and parts[0].startswith("NOT "):
        return ParsedQuery(expression="", terms=terms, negated=negated)
    return ParsedQuery(expression=" ".join(parts), terms=terms, negated=negated)


def _literal(value: str) -> str:
    return '"' + _UNSAFE.sub(lambda m: '""' if m.group() == '"' else " ", value) + '"'


# ── maintaining the index ────────────────────────────────────────────────


def index_capture(
    session: Session,
    settings: Settings,
    *,
    site: Site,
    capture: Capture,
    pages: list[textextract.Page] | None = None,
) -> int:
    """Put one capture's pages into the index, replacing older versions.

    Rows are keyed on (site, url), so a page captured again supersedes itself
    rather than accumulating a copy per capture. That is what makes a search
    result "the archive's current answer" instead of a list of every time the
    crawler saw the same page.
    """
    if pages is None:
        pages = list(textextract.read_pages(settings, site.archive_path, capture.dir_name))
    if not pages:
        return 0

    stamp = utcnow()
    indexed = 0
    for page in pages:
        body = page.text
        if not body and not page.title:
            continue
        row = session.scalar(
            select(PageText).where(PageText.site_id == site.id, PageText.url == page.url)
        )
        if row is None:
            row = PageText(site_id=site.id, url=page.url)
            session.add(row)
            session.flush()
        else:
            _fts_delete(session, [row.id])
        row.capture_id = capture.id
        row.capture_dir = capture.dir_name
        row.title = page.title or None
        row.timestamp = _cdx_timestamp(page.timestamp)
        row.offset = page.offset
        row.length = page.length
        row.words = len(body.split())
        row.indexed_at = stamp
        session.flush()
        _fts_insert(session, row.id, page.title, body)
        indexed += 1

    session.flush()
    return indexed


def drop_capture(session: Session, capture_id: int) -> int:
    """Forget everything indexed from one capture."""
    ids = list(session.scalars(select(PageText.id).where(PageText.capture_id == capture_id)).all())
    if not ids:
        return 0
    _fts_delete(session, ids)
    session.execute(delete(PageText).where(PageText.id.in_(ids)))
    session.flush()
    return len(ids)


def drop_site(session: Session, site_id: int) -> int:
    ids = list(session.scalars(select(PageText.id).where(PageText.site_id == site_id)).all())
    if not ids:
        return 0
    _fts_delete(session, ids)
    session.execute(delete(PageText).where(PageText.id.in_(ids)))
    session.flush()
    return len(ids)


def reindex_site(session: Session, settings: Settings, site: Site) -> int:
    """Rebuild a site's index from the text already on disk.

    Oldest capture first, so the newest version of each page is the one that
    survives. No WARC is read: that is the point of keeping the extracted text
    as a file rather than only in the database.
    """
    drop_site(session, site.id)
    captures = list(
        session.scalars(
            select(Capture)
            .where(Capture.site_id == site.id)
            .order_by(Capture.started_at.asc(), Capture.id.asc())
        ).all()
    )
    total = 0
    for capture in captures:
        total += index_capture(session, settings, site=site, capture=capture)
    return total


# A virtual table's name cannot be a bind parameter, so these three
# statements interpolate `FTS_TABLE` — a module constant, never anything that
# reaches this process from outside. Every value in them is still bound.
#
# bm25() is negative and more negative is better, so ascending is best-first.
# Title matches are weighted well above body ones: a term in the title of a
# post is almost always what somebody meant.
_MATCH_SQL = (
    f"SELECT rowid AS rid, bm25({FTS_TABLE}, 10.0, 1.0) AS score "  # noqa: S608
    f"FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH :q ORDER BY score LIMIT :cap"
)


def _fts_insert(session: Session, rowid: int, title: str | None, body: str) -> None:
    session.execute(
        text(f"INSERT INTO {FTS_TABLE}(rowid, title, body) VALUES (:rowid, :title, :body)"),  # noqa: S608
        {"rowid": rowid, "title": title or "", "body": body},
    )


def _fts_delete(session: Session, rowids: list[int]) -> None:
    for rowid in rowids:
        session.execute(text(f"DELETE FROM {FTS_TABLE} WHERE rowid = :rowid"), {"rowid": rowid})  # noqa: S608


def _cdx_timestamp(warc_date: str) -> str:
    """`2026-08-11T19:14:34Z` → `20260811191434`, which is what replay wants."""
    digits = re.sub(r"\D", "", warc_date or "")
    return digits[:14]


# ── searching ────────────────────────────────────────────────────────────


def search(
    session: Session,
    settings: Settings,
    *,
    query: str,
    site_id: int | None = None,
    folder: str | None = None,
    tag: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> Results:
    parsed = parse_query(query)
    if parsed.empty:
        return Results(hits=[], total=0, query=query, terms=parsed.terms)

    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    offset = max(0, int(offset or 0))

    try:
        rows = session.execute(
            text(_MATCH_SQL), {"q": parsed.expression, "cap": CANDIDATE_LIMIT}
        ).all()
    except Exception as exc:
        raise SearchError(str(exc)) from exc

    if not rows:
        return Results(hits=[], total=0, query=query, terms=parsed.terms)

    scores = {int(r.rid): float(r.score) for r in rows}
    stmt = (
        select(PageText, Site)
        .join(Site, Site.id == PageText.site_id)
        .where(PageText.id.in_(list(scores)), Site.deleted_at.is_(None))
    )
    if site_id is not None:
        stmt = stmt.where(PageText.site_id == site_id)
    if folder:
        from cairn.db.models import Folder

        stmt = stmt.join(Folder, Folder.id == Site.folder_id).where(
            (Folder.path == folder) | (Folder.path.startswith(f"{folder}/"))
        )
    if tag:
        stmt = stmt.where(
            PageText.site_id.in_(
                select(SiteTag.site_id).join(Tag, Tag.id == SiteTag.tag_id).where(Tag.slug == tag)
            )
        )

    pairs = list(session.execute(stmt).all())
    pairs.sort(key=lambda pair: scores.get(pair[0].id, 0.0))
    total = len(pairs)
    window = pairs[offset : offset + limit]

    folders = _folder_paths(session, {site.folder_id for _row, site in window})
    hits = [
        Hit(
            site_id=site.id,
            site_title=site.title,
            site_slug=site.slug,
            folder_path=folders.get(site.folder_id, ""),
            url=row.url,
            title=row.title or row.url,
            snippets=snippets_for(settings, site, row, parsed.terms),
            score=round(-scores.get(row.id, 0.0), 4),
            capture_id=row.capture_id,
            timestamp=row.timestamp,
            words=row.words,
        )
        for row, site in window
    ]
    return Results(
        hits=hits,
        total=total,
        query=query,
        terms=parsed.terms,
        truncated=len(rows) >= CANDIDATE_LIMIT,
    )


def _folder_paths(session: Session, folder_ids: set[int]) -> dict[int, str]:
    if not folder_ids:
        return {}
    from cairn.db.models import Folder

    rows = session.execute(select(Folder.id, Folder.path).where(Folder.id.in_(folder_ids))).all()
    return {int(fid): str(path) for fid, path in rows}


def snippets_for(settings: Settings, site: Site, row: PageText, terms: list[str]) -> list[str]:
    """The lines around each term, read from the file the extractor wrote.

    Contentless FTS5 cannot do this — `snippet()` is NULL — and doing it here
    has a compensating advantage: the surrounding text is the extracted,
    de-boilerplated version, so the excerpt reads like the page rather than
    like its navigation.
    """
    try:
        path = textextract.text_path(settings, site.archive_path, row.capture_dir)
    except storage.StoragePathError:  # pragma: no cover — capture_dir is ours
        return []
    page = textextract.read_page_at(path, row.offset, row.length)
    if page is None:
        return []
    return excerpt(page.text, terms)


def excerpt(body: str, terms: list[str], *, radius: int = SNIPPET_RADIUS) -> list[str]:
    """Up to `MAX_SNIPPETS` windows of text around the terms, in order."""
    if not body:
        return []
    lowered = body.lower()
    spans: list[tuple[int, int]] = []
    for term in terms:
        needle = term.lower()
        if not needle:
            continue
        start = lowered.find(needle)
        if start < 0:
            continue
        spans.append((max(0, start - radius), min(len(body), start + len(needle) + radius)))

    if not spans:
        return [_tidy(body[: radius * 2])]

    spans.sort()
    merged: list[tuple[int, int]] = []
    for span in spans:
        if merged and span[0] <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], span[1]))
        else:
            merged.append(span)

    out = []
    for start, end in merged[:MAX_SNIPPETS]:
        piece = _tidy(body[start:end])
        out.append(("…" if start > 0 else "") + piece + ("…" if end < len(body) else ""))
    return out


def _tidy(piece: str) -> str:
    return " ".join(piece.split())


# ── reporting ────────────────────────────────────────────────────────────


def stats(session: Session) -> dict[str, Any]:
    pages = session.scalar(select(func.count(PageText.id))) or 0
    words = session.scalar(select(func.sum(PageText.words))) or 0
    sites = session.scalar(select(func.count(func.distinct(PageText.site_id)))) or 0
    return {"pages": int(pages), "words": int(words), "sites": int(sites)}


def unindexed_sites(session: Session) -> list[int]:
    """Sites with captures but nothing in the index — the reindex prompt."""
    indexed = select(PageText.site_id).distinct()
    return list(
        session.scalars(
            select(Site.id)
            .join(Capture, Capture.site_id == Site.id)
            .where(Site.deleted_at.is_(None), Site.id.notin_(indexed))
            .distinct()
        ).all()
    )


def text_size(settings: Settings, site: Site) -> int:
    root = (
        storage.site_dir(settings, site.archive_path) / storage.DERIVED_DIR / textextract.TEXT_DIR
    )
    return storage.directory_size(Path(root))
