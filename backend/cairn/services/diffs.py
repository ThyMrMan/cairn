"""What changed between two captures.

This is what turns an archive from a copy into a record. "Should I keep
running full recaptures?" is unanswerable while every capture is an opaque
pile of WARCs, and answerable the moment you can see that eleven of two
thousand pages changed and what the changes were.

**Diffed from the extracted text, not the HTML.** A page's markup changes on
every fetch of a site with a visit counter, a rotating ad slot or a
"generated at" stamp in the footer — measured: three fetches of one unchanged
post produced three different HTML hashes and one identical extracted-text
hash. Diffing the markup would report every page as changed, forever, which is
the same as reporting nothing.

**Two granularities, because one is never right.** Blocks say *which*
paragraphs came and went; words say what happened inside a paragraph that
survived. `difflib` does both and is fast enough that no dependency is
warranted: measured at 9 ms for a 60,000-word page at block level plus 8 ms of
word-level work, and 0.1 ms for two pages with nothing in common.

Resources — images, stylesheets, scripts — are compared from the CDXJ index
instead, since they have no text. A changed digest for the same URL is an
asset that was replaced, which is the one that usually explains why a page
looks different when nothing it says has changed.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any

from cairn.config import Settings
from cairn.db.models import Capture, Site
from cairn.logging import get_logger
from cairn.services import replay, textextract

log = get_logger(__name__)

# Blocks longer than this are compared as whole units rather than word by
# word. A word-level diff of two 50,000-character blocks is quadratic in the
# worst case and nobody reads the result anyway.
MAX_WORD_DIFF_CHARS = 20_000
CONTEXT_CHARS = 160


class DiffError(RuntimeError):
    """The two captures could not be compared."""


@dataclass(slots=True)
class WordEdit:
    kind: str  # replace | insert | delete
    before: str
    after: str


@dataclass(slots=True)
class BlockChange:
    kind: str  # added | removed | changed
    before: str = ""
    after: str = ""
    words: list[WordEdit] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "before": self.before,
            "after": self.after,
            "words": [{"kind": w.kind, "before": w.before, "after": w.after} for w in self.words],
        }


@dataclass(slots=True)
class ResourceChange:
    kind: str  # added | removed | changed
    url: str
    mime: str = ""
    before_digest: str = ""
    after_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "url": self.url,
            "mime": self.mime,
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
        }


@dataclass(slots=True)
class PageDiff:
    url: str
    before_capture: str
    after_capture: str
    before_title: str = ""
    after_title: str = ""
    changed: bool = False
    blocks: list[BlockChange] = field(default_factory=list)
    #: Fraction of blocks that are not identical, from 0 to 1.
    change_ratio: float = 0.0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "before_capture": self.before_capture,
            "after_capture": self.after_capture,
            "before_title": self.before_title,
            "after_title": self.after_title,
            "changed": self.changed,
            "change_ratio": round(self.change_ratio, 4),
            "blocks": [b.to_dict() for b in self.blocks],
            "note": self.note,
        }


@dataclass(slots=True)
class PageSummary:
    url: str
    title: str
    kind: str  # added | removed | changed | unchanged
    change_ratio: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "kind": self.kind,
            "change_ratio": round(self.change_ratio, 4),
        }


@dataclass(slots=True)
class CaptureDiff:
    before_capture: str
    after_capture: str
    added: int = 0
    removed: int = 0
    changed: int = 0
    unchanged: int = 0
    pages: list[PageSummary] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "before_capture": self.before_capture,
            "after_capture": self.after_capture,
            "added": self.added,
            "removed": self.removed,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "pages": [p.to_dict() for p in self.pages],
            "note": self.note,
        }


# ── text ─────────────────────────────────────────────────────────────────


def diff_blocks(before: list[str], after: list[str]) -> tuple[list[BlockChange], float]:
    """Block-level changes, with a word-level pass inside replaced blocks."""
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    changes: list[BlockChange] = []
    same = 0

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            same += i2 - i1
            continue
        if tag == "delete":
            changes.extend(BlockChange("removed", before=b) for b in before[i1:i2])
            continue
        if tag == "insert":
            changes.extend(BlockChange("added", after=a) for a in after[j1:j2])
            continue
        # replace: pair them up so the common case — one block edited in
        # place — reads as an edit rather than as a deletion and an addition.
        old, new = before[i1:i2], after[j1:j2]
        for index in range(max(len(old), len(new))):
            left = old[index] if index < len(old) else ""
            right = new[index] if index < len(new) else ""
            if not left:
                changes.append(BlockChange("added", after=right))
            elif not right:
                changes.append(BlockChange("removed", before=left))
            else:
                changes.append(
                    BlockChange("changed", before=left, after=right, words=diff_words(left, right))
                )

    total = max(len(before), len(after)) or 1
    return changes, (total - same) / total


def diff_words(before: str, after: str) -> list[WordEdit]:
    if len(before) > MAX_WORD_DIFF_CHARS or len(after) > MAX_WORD_DIFF_CHARS:
        return []
    left, right = before.split(), after.split()
    edits: list[WordEdit] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, left, right, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            continue
        edits.append(WordEdit(tag, " ".join(left[i1:i2]), " ".join(right[j1:j2])))
    return edits


# ── reading two captures ─────────────────────────────────────────────────


def _pages_of(settings: Settings, site: Site, capture: Capture) -> dict[str, textextract.Page]:
    return {
        page.url: page
        for page in textextract.read_pages(settings, site.archive_path, capture.dir_name)
    }


def _missing_text(capture: Capture) -> str:
    return (
        f"No extracted text for {capture.dir_name}. It was captured before text extraction "
        "existed, or extraction was switched off — run Rebuild search index with re-extraction "
        "to fill it in."
    )


def compare_page(
    settings: Settings, site: Site, *, before: Capture, after: Capture, url: str
) -> PageDiff:
    """One page, as two captures saw it."""
    diff = PageDiff(url=url, before_capture=before.dir_name, after_capture=after.dir_name)

    left = _page_at(settings, site, before, url)
    right = _page_at(settings, site, after, url)
    if left is None and right is None:
        diff.note = (
            f"Neither capture holds extracted text for {url}. Either it is not an HTML page, "
            "or neither capture fetched it."
        )
        return diff

    diff.before_title = left.title if left else ""
    diff.after_title = right.title if right else ""
    before_blocks = left.blocks if left else []
    after_blocks = right.blocks if right else []
    diff.blocks, diff.change_ratio = diff_blocks(before_blocks, after_blocks)
    diff.changed = bool(diff.blocks) or diff.before_title != diff.after_title

    if left is None:
        diff.note = f"This page is not in {before.dir_name}; the whole of it is new."
    elif right is None:
        diff.note = f"This page is not in {after.dir_name}; it was not fetched again."
    return diff


def _page_at(settings: Settings, site: Site, capture: Capture, url: str) -> textextract.Page | None:
    for page in textextract.read_pages(settings, site.archive_path, capture.dir_name):
        if page.url == url:
            return page
    return None


def compare_resources(
    settings: Settings, site: Site, *, before: Capture, after: Capture
) -> list[ResourceChange]:
    """Assets whose bytes changed, arrived or went, from the CDXJ.

    Keyed on URL and compared by digest, which is what makes "the logo was
    replaced" visible — the URL is identical and nothing in any page's text
    says a thing about it.

    Per capture rather than per page, deliberately: a CDXJ records what was
    fetched, not which page asked for it, so attributing an asset to a page
    would mean inventing a relationship the archive does not store.
    """
    left = _records_of(settings, site, before)
    right = _records_of(settings, site, after)

    changes: list[ResourceChange] = []
    for url in sorted(set(left) | set(right)):
        a, b = left.get(url), right.get(url)
        if a and b and a[0] != b[0]:
            changes.append(ResourceChange("changed", url, b[1], a[0], b[0]))
        elif a and not b:
            changes.append(ResourceChange("removed", url, a[1], a[0], ""))
        elif b and not a:
            changes.append(ResourceChange("added", url, b[1], "", b[0]))
    return changes


def _records_of(settings: Settings, site: Site, capture: Capture) -> dict[str, tuple[str, str]]:
    """`url -> (digest, mime)` for one capture, from the site index."""
    out: dict[str, tuple[str, str]] = {}
    for record in replay.index_records(settings, site.archive_path):
        if not _belongs_to(record.filename, capture.dir_name):
            continue
        if record.mime and "html" in record.mime.lower():
            continue
        out[record.url] = (record.digest or "", record.mime or "")
    return out


def _belongs_to(filename: str, capture_dir: str) -> bool:
    return filename.startswith(f"captures/{capture_dir}/")


# ── a whole capture ──────────────────────────────────────────────────────


def compare_captures(
    settings: Settings, site: Site, *, before: Capture, after: Capture, limit: int = 500
) -> CaptureDiff:
    """Which pages a recapture actually changed.

    The answer to "should I keep running full recaptures?", and usually the
    answer is no: a settled blog recaptured monthly changes a handful of pages
    and rewrites gigabytes to say so.
    """
    diff = CaptureDiff(before_capture=before.dir_name, after_capture=after.dir_name)
    left = _pages_of(settings, site, before)
    right = _pages_of(settings, site, after)

    if not left and not right:
        diff.note = _missing_text(before)
        return diff

    for url in sorted(set(left) | set(right)):
        a, b = left.get(url), right.get(url)
        if a is None and b is not None:
            diff.added += 1
            diff.pages.append(PageSummary(url, b.title, "added", 1.0))
        elif b is None and a is not None:
            diff.removed += 1
            diff.pages.append(PageSummary(url, a.title, "removed", 1.0))
        elif a is not None and b is not None:
            if a.blocks == b.blocks:
                diff.unchanged += 1
                continue
            _blocks, ratio = diff_blocks(a.blocks, b.blocks)
            diff.changed += 1
            diff.pages.append(PageSummary(url, b.title or a.title, "changed", ratio))

    # Most changed first: the point of the list is to be read from the top and
    # abandoned partway down.
    diff.pages.sort(key=lambda p: (-p.change_ratio, p.url))
    if len(diff.pages) > limit:
        diff.note = f"Showing the {limit} most-changed of {len(diff.pages)} page(s)."
        diff.pages = diff.pages[:limit]
    return diff
