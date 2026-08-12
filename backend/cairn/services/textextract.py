"""Readable text out of archived HTML, so the archive can be searched.

Three things decide the quality of a search index over a blog, and only one of
them is the search engine.

**Boilerplate is the whole problem.** A blog's sidebar lists every post title
on every page. Index that and one post title matches all two thousand pages,
which is not a ranking problem — the result list is simply wrong. So this
module's job is less "get the text out" than "leave the furniture behind".

**Two filters, because either alone fails.** Class and id names catch the
common templates: Blogger writes `class='sidebar section'`, WordPress writes
`class="widget-area"`, and both announce their nav and footer plainly. A
hand-rolled template that names its columns `left` and `right` announces
nothing, and there the second filter takes over: we index a whole capture at
once, so boilerplate is *the blocks that appear on most of the pages*. A
sidebar is byte-identical across the site; an article is not.

Measured against `trafilatura` on Blogger and WordPress markup, the class
rules alone match it — same article, no sidebar, no nav — and additionally
keep the `<title>`, which trafilatura's extractor drops. On a template with no
usable class names trafilatura wins and the class rules leak the entire
sidebar; the repetition pass closes that, and closes it better, because it
knows the corpus. The one case with neither signal is a capture of a single
page, where "this block is on every page" cannot mean anything — and where a
sidebar shared with pages nobody captured does no harm either.

That is why there is no HTML parsing dependency here. `trafilatura` brings
lxml, justext, courlan, htmldate and dateparser to do a job the stdlib parser
plus knowledge of the corpus does at least as well on the pages this tool
actually archives.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from cairn.config import Settings
from cairn.logging import get_logger
from cairn.services import storage

log = get_logger(__name__)

TEXT_DIR = "text"

# Read this much of a response before giving up on it. Generous next to the
# 512 KB the asset audit reads, because that one only needs the markup near
# the top and this one needs the article.
MAX_PAGE_BYTES = 4 * 1024 * 1024
# Keep this much text per page. A page that hits this is a data dump rather
# than a document, and indexing all of it would let one URL dominate.
MAX_TEXT_CHARS = 120_000
# Blocks shorter than this are navigation crumbs, dates and byline fragments;
# they add noise and never carry the sentence somebody is searching for.
MIN_BLOCK_CHARS = 12

# Drop a block that appears on at least this share of the capture's pages,
# never on fewer than this many. Full-template furniture sits at ~100%, so the
# threshold only has to be high enough that repeated *content* is safe: two
# pages agreeing proves nothing, half a site agreeing is a template.
REPEAT_SHARE = 0.5
REPEAT_MIN_PAGES = 3

SKIP_ELEMENTS = frozenset(
    {"script", "style", "noscript", "template", "svg", "head", "iframe", "object", "select"}
)
BOILERPLATE_TAGS = frozenset({"nav", "aside", "footer", "form"})
BLOCK_TAGS = frozenset(
    {
        "p", "div", "section", "article", "main", "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "tr", "td", "th", "blockquote", "pre", "br", "hr", "figcaption", "dd",
        "dt", "ul", "ol", "table", "header", "footer", "nav", "aside",
    }
)  # fmt: skip

# What a block is *for*, as far as reading it goes. Deliberately a handful of
# values rather than the tag: the reader only needs to know whether something
# is a heading, an item, a quotation or prose, and storing "h5" so it can be
# rendered as an h3 anyway is a longer file for no difference on screen.
BLOCK_KINDS = {
    "h1": "h1", "h2": "h2", "h3": "h3", "h4": "h3", "h5": "h3", "h6": "h3",
    "li": "li", "dd": "li", "dt": "li",
    "blockquote": "quote", "pre": "pre", "figcaption": "caption",
}  # fmt: skip

# Words that appear in the class or id of a region no reader would call the
# page. Matched as whole words within the attribute, so `post-body` survives
# while `post-footer` does not.
_BOILER_WORDS = (
    "navbar|nav|navigation|menu|sidebar|side|footer|masthead|header|comments?|share|social"
    "|related|breadcrumbs?|pagination|pager|widget-area|banner|cookie|skip-link"
    "|screen-reader|crosscol|attribution|site-info|post-footer|entry-footer|entry-meta"
    "|blog-pager|tabs|toolbar|subscribe|newsletter|advert|ads?|promo|popup|modal"
)
BOILERPLATE_HINT = re.compile(rf"(?:^|[\s_-])(?:{_BOILER_WORDS})(?:$|[\s_-])", re.IGNORECASE)
# Checked first, and wins: these contain a boilerplate word and are the page.
# `entry-header` is the one that matters — WordPress puts the post title
# inside it, so matching `header` there would drop the title of every post.
CONTENT_HINT = re.compile(
    r"(?:^|[\s_-])(?:post-body|post-content|post-header|post-title|entry-content|entry-body"
    r"|entry-header|entry-title|article-body|article-header|content)(?:$|[\s_-])",
    re.IGNORECASE,
)

_CHARSET_META = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_.:-]+)""", re.IGNORECASE
)
_WS = re.compile(r"\s+")


class TextExtractError(RuntimeError):
    """The capture's text could not be extracted."""


@dataclass(slots=True)
class Page:
    """One archived page as the search index knows it."""

    url: str
    title: str
    blocks: list[str] = field(default_factory=list)
    # What each block was, one per block: "h1".."h3", "li", "quote", "pre", or
    # "p". Search ignores it entirely; the reader view needs it, because text
    # in which every heading is a paragraph is markedly harder to read than
    # the page it came from. Absent from files written before it existed, and
    # `kind_of` falls back rather than making those files unreadable.
    kinds: list[str] = field(default_factory=list)
    timestamp: str = ""
    # Byte offset and length of this page's line in the capture's JSONL, so a
    # snippet is a seek and a read rather than a scan of the whole file.
    offset: int = 0
    length: int = 0

    @property
    def text(self) -> str:
        return "\n".join(self.blocks)

    def kind_of(self, index: int) -> str:
        """One block's kind, defaulting to a paragraph.

        Indexed rather than zipped so a file written before `kinds` existed —
        or one where the two lists have drifted — reads as prose instead of
        raising or losing blocks.
        """
        if 0 <= index < len(self.kinds):
            return self.kinds[index] or "p"
        return "p"


@dataclass(slots=True)
class ExtractResult:
    path: Path | None
    pages: list[Page]
    scanned: int
    dropped_blocks: int


# ── the parser ───────────────────────────────────────────────────────────


class _BlockParser(HTMLParser):
    """Text split into blocks, with the furniture left out.

    Blocks rather than one string because the repetition filter needs
    something to compare, and because a block boundary is the only thing
    keeping a heading from running into the paragraph after it.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.kinds: list[str] = []
        self.title = ""
        # What the block currently being buffered was opened by. One value
        # rather than a stack: a stack has to survive unbalanced markup, which
        # archived pages are full of, and the worst a single value gets wrong
        # is that `<li><p>x</p></li>` reads as a paragraph — which it is.
        self._kind = "p"
        self._buf: list[str] = []
        self._skip = 0
        self._depth = 0
        # Depths at which a boilerplate region opened. A list rather than a
        # counter so a `</div>` closes exactly the region it opened, however
        # much unbalanced markup sits in between.
        self._boiler: list[int] = []
        self._in_title = False
        self._chars = 0

    # HTMLParser calls these; the names are its interface, not ours.
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_ELEMENTS:
            self._skip += 1
            return
        if tag == "title":
            self._in_title = True
            return
        self._depth += 1
        if not self._boiler and _is_boilerplate(tag, attrs):
            self._boiler.append(self._depth)
        if tag in BLOCK_TAGS:
            self._flush()
            self._kind = BLOCK_KINDS.get(tag, "p")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_ELEMENTS:
            self._skip = max(0, self._skip - 1)
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in BLOCK_TAGS:
            self._flush()
        while self._boiler and self._boiler[-1] >= self._depth:
            self._boiler.pop()
        self._depth = max(0, self._depth - 1)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip and not self._boiler and self._chars < MAX_TEXT_CHARS:
            self._buf.append(data)
            self._chars += len(data)

    def finish(self) -> None:
        """Flush whatever is buffered — including after a parse gave up."""
        self._flush()

    def _flush(self) -> None:
        text = _WS.sub(" ", "".join(self._buf)).strip()
        self._buf.clear()
        if len(text) >= MIN_BLOCK_CHARS:
            self.blocks.append(text)
            self.kinds.append(self._kind)
        self._kind = "p"


def _is_boilerplate(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
    values = [v for name, v in attrs if name in ("class", "id", "role") and v]
    if any(CONTENT_HINT.search(v) for v in values):
        return False
    if tag in BOILERPLATE_TAGS:
        return True
    return any(BOILERPLATE_HINT.search(v) for v in values)


def parse(html: str) -> tuple[str, list[str]]:
    """`(title, blocks)` for one page."""
    title, blocks, _kinds = parse_kinds(html)
    return title, blocks


def parse_kinds(html: str) -> tuple[str, list[str], list[str]]:
    """`(title, blocks, kinds)` — the same pass, with what each block was."""
    parser = _BlockParser()
    try:
        parser.feed(html)
    except Exception as exc:
        log.debug("html parse gave up", extra={"err": str(exc)})
    parser.finish()
    return _WS.sub(" ", parser.title).strip(), parser.blocks, parser.kinds


def decode(body: bytes, content_type: str) -> str:
    """Bytes to text, believing the header, then the document, then UTF-8.

    Never raises: an archive holds pages in encodings that were already wrong
    when they were served, and a page that decodes imperfectly is still worth
    searching.
    """
    charsets: list[str] = []
    match = re.search(r"charset\s*=\s*([a-zA-Z0-9_.:-]+)", content_type or "", re.IGNORECASE)
    if match:
        charsets.append(match.group(1))
    meta = _CHARSET_META.search(body[:4096])
    if meta:
        charsets.append(meta.group(1).decode("ascii", "ignore"))
    charsets.append("utf-8")

    for charset in charsets:
        try:
            return body.decode(charset)
        except (LookupError, UnicodeDecodeError, ValueError):
            continue
    return body.decode("utf-8", errors="replace")


# ── walking a capture ────────────────────────────────────────────────────


def text_path(settings: Settings, archive_path: str, capture_dir: str) -> Path:
    root = storage.site_dir(settings, archive_path) / storage.DERIVED_DIR / TEXT_DIR
    return storage.resolve_within(root, f"{capture_dir}.jsonl")


def _warcs(settings: Settings, archive_path: str, capture_dir: str) -> list[Path]:
    root = storage.site_dir(settings, archive_path) / storage.CAPTURES_DIR / capture_dir
    warc_dir = root / storage.WARC_DIR
    if not warc_dir.is_dir():
        return []
    return sorted(warc_dir.glob("*.warc.gz")) + sorted(warc_dir.glob("*.warc"))


def read_pages(settings: Settings, archive_path: str, capture_dir: str) -> Iterator[Page]:
    """Every page of a capture, from the JSONL rather than the WARC.

    This is what makes the search index derived data rather than a second copy
    of the archive: rebuilding it is a pass over a few megabytes of text, not
    a re-parse of gigabytes of WARC.
    """
    path = text_path(settings, archive_path, capture_dir)
    if not path.is_file():
        return
    with open(path, "rb") as fh:
        offset = 0
        for raw in fh:
            length = len(raw)
            try:
                doc = json.loads(raw)
            except ValueError:
                offset += length
                continue
            yield Page(
                url=str(doc.get("url") or ""),
                title=str(doc.get("title") or ""),
                blocks=[str(b) for b in (doc.get("blocks") or [])],
                kinds=[str(k) for k in (doc.get("kinds") or [])],
                timestamp=str(doc.get("ts") or ""),
                offset=offset,
                length=length,
            )
            offset += length


def read_page_at(path: Path, offset: int, length: int) -> Page | None:
    """One page, by the offset recorded when it was indexed."""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            raw = fh.read(length)
        doc = json.loads(raw)
    except (OSError, ValueError):
        return None
    return Page(
        url=str(doc.get("url") or ""),
        title=str(doc.get("title") or ""),
        blocks=[str(b) for b in (doc.get("blocks") or [])],
        kinds=[str(k) for k in (doc.get("kinds") or [])],
        timestamp=str(doc.get("ts") or ""),
        offset=offset,
        length=length,
    )


def extract_capture(settings: Settings, archive_path: str, capture_dir: str) -> ExtractResult:
    """Extract every HTML page of one capture into `derived/text/<capture>.jsonl`.

    One pass over the WARCs, because they are the large thing. The repetition
    filter needs the whole capture before it can decide anything, so pages are
    held as blocks in memory — text only, no markup, and bounded per page —
    and written once the counts are known.
    """
    try:
        from warcio.archiveiterator import ArchiveIterator
    except ImportError as exc:  # pragma: no cover — declared in pyproject
        raise TextExtractError("warcio is not installed; text cannot be extracted") from exc

    warcs = _warcs(settings, archive_path, capture_dir)
    if not warcs:
        return ExtractResult(path=None, pages=[], scanned=0, dropped_blocks=0)

    # Keyed by URL: a WARC can hold the same page twice, and the later record
    # is the one the index will serve.
    pages: dict[str, Page] = {}
    scanned = 0
    for warc in warcs:
        try:
            with open(warc, "rb") as fh:
                for record in ArchiveIterator(fh):
                    page = _page_from(record)
                    if page is None:
                        continue
                    scanned += 1
                    pages[page.url] = page
        except Exception as exc:
            log.warning(
                "text extraction could not read a WARC",
                extra={"warc": warc.name, "err": str(exc)},
            )
            continue

    ordered = list(pages.values())
    dropped = _drop_repeated(ordered)
    ordered = [p for p in ordered if p.blocks]

    path = text_path(settings, archive_path, capture_dir)
    _write_jsonl(path, ordered)
    return ExtractResult(path=path, pages=ordered, scanned=scanned, dropped_blocks=dropped)


def _page_from(record: Any) -> Page | None:
    if record.rec_type != "response" or not record.http_headers:
        return None
    status = record.http_headers.get_statuscode() or ""
    if not str(status).startswith("2"):
        return None
    ctype = record.http_headers.get_header("Content-Type", "") or ""
    if "html" not in ctype.lower():
        return None
    url = record.rec_headers.get_header("WARC-Target-URI") or ""
    if not url:
        return None
    body = record.content_stream().read(MAX_PAGE_BYTES)
    title, blocks, kinds = parse_kinds(decode(body, ctype))
    if not blocks and not title:
        return None
    return Page(
        url=url,
        title=title,
        blocks=blocks,
        kinds=kinds,
        timestamp=str(record.rec_headers.get_header("WARC-Date") or ""),
    )


def _drop_repeated(pages: list[Page]) -> int:
    """Remove blocks that appear on most of the capture's pages.

    Returns how many block occurrences went, which is what makes the effect
    visible in the capture report rather than something to take on trust.
    """
    if len(pages) < REPEAT_MIN_PAGES:
        return 0
    threshold = max(REPEAT_MIN_PAGES, int(len(pages) * REPEAT_SHARE))

    counts: Counter[str] = Counter()
    for page in pages:
        counts.update({_key(b) for b in page.blocks})
    common = {key for key, n in counts.items() if n >= threshold}
    if not common:
        return 0

    dropped = 0
    for page in pages:
        # Filtered in lockstep: the two lists are positional, so dropping a
        # block without its kind silently shifts every heading after it.
        keep = [i for i, b in enumerate(page.blocks) if _key(b) not in common]
        dropped += len(page.blocks) - len(keep)
        page.kinds = [page.kind_of(i) for i in keep]
        page.blocks = [page.blocks[i] for i in keep]
    return dropped


def _key(block: str) -> str:
    return hashlib.blake2b(block.encode("utf-8", "replace"), digest_size=16).hexdigest()


def _write_jsonl(path: Path, pages: list[Page]) -> None:
    """Write the pages and record where each one landed.

    Written whole and renamed into place, like every other derived file — a
    half-written text file would be read by the indexer as a capture that
    stops halfway through the alphabet.
    """
    chunks: list[bytes] = []
    offset = 0
    for page in pages:
        raw = (
            json.dumps(
                {
                    "url": page.url,
                    "title": page.title,
                    "ts": page.timestamp,
                    "blocks": page.blocks,
                    "kinds": page.kinds,
                },
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
        page.offset = offset
        page.length = len(raw)
        offset += len(raw)
        chunks.append(raw)
    storage.write_atomic(path, b"".join(chunks))


def remove_capture_text(settings: Settings, archive_path: str, capture_dir: str) -> None:
    # Best effort: the capture is going whether or not its derived text does,
    # and a leftover text file is regenerated by the next reindex.
    with contextlib.suppress(OSError, storage.StoragePathError):
        text_path(settings, archive_path, capture_dir).unlink(missing_ok=True)
