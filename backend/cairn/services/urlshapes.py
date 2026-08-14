"""What a capture is actually fetching, grouped by the shape of the URL.

The question this answers is "my index said 38,000 pages and the crawl is past
140,000 URLs — on what?", and until now the only way to ask it was to guess a
substring and count matches. Guessing works when you already suspect the
answer; it is useless when you do not, and it is silently misleading when the
thing you guessed appears in a hundred thousand URLs for a reason you did not
imagine.

**Segments are collapsed by cardinality, not by looking at them one at a
time.** A per-URL heuristic cannot tell that `/search/label/Travel` and
`/search/label/Food` are the same shape: both segments are short words, and
nothing about `Travel` in isolation says "this is a value, not a path". Across
the whole capture it is obvious — under `/search/label/` there are four hundred
different values — so this reads the capture twice: once to learn where the
variation is, once to build the shapes. That is what turns several hundred
label-archive URLs into a single row saying `/search/label/*` with the count
beside it.

**Cardinality is counted per prefix, not per depth**, and the difference is not
subtle. Keyed by depth alone, `/search/label/Travel` and `/img/photo-3.jpg`
share "depth 1", so forty image filenames and the single word `label` land in
one bucket, that bucket looks varied, and `label` gets collapsed into `*` —
destroying the one row the report exists to show. Each node of the path tree is
judged on its own children.

Both passes are bounded by a snapshot of the highest row id, because the case
this exists for is a crawl still in flight, and two passes over a growing table
would otherwise disagree with each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cairn.db.models import CaptureUrl

# Rows read per round trip. Large enough that a 400k-URL capture is not a
# million round trips, small enough that nothing holds the whole table.
CHUNK = 5_000

# A position is carrying identifiers rather than structure when its values are
# both numerous *and* varied relative to how often the position is used. Both
# halves are needed, and a fixed count for either is wrong:
#
#   /search/label/<label>   60 distinct across 480 uses   12%  -> a value
#   /img/<file>             40 distinct across  40 uses  100%  -> a value
#   /<section>/…             2 distinct across 561 uses    0%  -> structure
#
# An absolute cutoff alone gets the middle row wrong on a small capture and
# right on a large one — which is the worst kind of wrong, because it means the
# report changes character as a crawl grows.
VARY_FLOOR = 8
VARY_RATIO = 0.05

# Stop growing a position's value set once it is unambiguously an identifier.
# The verdict cannot change after this, and the set would otherwise hold one
# entry per post.
MAX_TRACKED = 2_000

# Ceiling on tree nodes, so a crawl whose *first* segment explodes cannot turn
# this report into a memory problem of its own.
MAX_NODES = 50_000

DIGITS = "0123456789"


@dataclass(slots=True)
class Shape:
    shape: str
    count: int = 0
    bytes: int = 0
    example: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "shape": self.shape,
            "count": self.count,
            "bytes": self.bytes,
            "example": self.example,
        }


def _is_numberish(segment: str) -> bool:
    """A path segment that is an id, a year, a date — not a name.

    Checked before cardinality so that `/2019/05/` collapses even on a blog
    with only three years of posts, where the year position would otherwise
    look like structure.
    """
    if not segment:
        return False
    digits = sum(1 for c in segment if c in DIGITS)
    return digits == len(segment) or digits >= len(segment) * 0.6


def _segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def _query_keys(query: str) -> list[str]:
    if not query:
        return []
    return sorted({key for key, _ in parse_qsl(query, keep_blank_values=True) if key})


def _extension(segment: str) -> str:
    _, dot, ext = segment.rpartition(".")
    return ext.lower() if dot and 1 <= len(ext) <= 5 and ext.isalnum() else ""


@dataclass(slots=True)
class Node:
    """One node of the path tree, and what its children looked like."""

    uses: int = 0
    children: set[str] = field(default_factory=set)
    saturated: bool = False

    def see(self, segment: str) -> None:
        self.uses += 1
        if self.saturated:
            return
        self.children.add(segment)
        if len(self.children) >= MAX_TRACKED:
            self.saturated = True

    @property
    def varies(self) -> bool:
        """Do this node's children look like values rather than structure?"""
        if self.saturated:
            return True
        distinct = len(self.children)
        return distinct > VARY_FLOOR and distinct > self.uses * VARY_RATIO


# Keyed by the normalised literal prefix, so `("search", "label")` is judged
# separately from `("img",)`.
Tree = dict[tuple[str, ...], Node]


def _normalise(segment: str) -> str:
    return "#" if _is_numberish(segment) else segment.lower()


def learn(urls: list[str], tree: Tree) -> None:
    """First pass: where in the path tree the variation actually is."""
    for url in urls:
        prefix: tuple[str, ...] = ()
        for segment in _segments(urlsplit(url).path):
            normalised = _normalise(segment)
            node = tree.get(prefix)
            if node is None:
                if len(tree) >= MAX_NODES:
                    # Out of budget. Missing nodes are read as varying below,
                    # which errs toward a shorter report rather than one with a
                    # hundred thousand rows in it.
                    break
                node = tree[prefix] = Node()
            node.see(normalised)
            prefix = (*prefix, normalised)


def shape_of(url: str, tree: Tree) -> str:
    """Second pass: the URL with its varying parts replaced."""
    parts = urlsplit(url)
    out: list[str] = []
    prefix: tuple[str, ...] = ()
    for segment in _segments(parts.path):
        normalised = _normalise(segment)
        if normalised == "#":
            out.append("#")
        else:
            node = tree.get(prefix)
            if node is not None and not node.varies:
                out.append(normalised)
            else:
                # Keep the extension: `*.html` and `*.jpg` under one prefix are
                # different answers to "what is this crawl doing".
                extension = _extension(segment)
                out.append(f"*.{extension}" if extension else "*")
        prefix = (*prefix, normalised)

    path = "/" + "/".join(out)
    keys = _query_keys(parts.query)
    return f"{path}?{'&'.join(keys)}" if keys else path


def summarize(session: Session, capture_id: int, *, limit: int = 30) -> dict[str, Any]:
    """Group one capture's URLs by shape, biggest first."""
    highest = session.scalar(
        select(func.max(CaptureUrl.id)).where(CaptureUrl.capture_id == capture_id)
    )
    if highest is None:
        return {"total": 0, "distinct_shapes": 0, "shapes": [], "truncated": False}

    base = select(CaptureUrl.url, CaptureUrl.size_bytes).where(
        CaptureUrl.capture_id == capture_id, CaptureUrl.id <= highest
    )

    tree: Tree = {}
    for chunk in session.execute(base.execution_options(yield_per=CHUNK)).partitions():
        learn([row[0] for row in chunk], tree)

    shapes: dict[str, Shape] = {}
    total = 0
    for chunk in session.execute(base.execution_options(yield_per=CHUNK)).partitions():
        for url, size in chunk:
            total += 1
            key = shape_of(url, tree)
            entry = shapes.get(key)
            if entry is None:
                entry = shapes[key] = Shape(shape=key, example=url)
            entry.count += 1
            entry.bytes += int(size or 0)

    ranked = sorted(shapes.values(), key=lambda s: (-s.count, s.shape))
    return {
        "total": total,
        "distinct_shapes": len(ranked),
        "shapes": [s.to_dict() for s in ranked[:limit]],
        "truncated": len(ranked) > limit,
    }
