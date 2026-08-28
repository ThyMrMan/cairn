"""Is this crawl converging, or is it going round?

A crawl that has stopped making progress looks exactly like one that is
working. The log scrolls, the counter rises, the rate holds steady, and the
job page reports all of it approvingly. The only thing that distinguishes the
two is whether the URLs are *new*, and nothing was asking.

The capture that prompted this ran for three days and never finished: 205,903
fetches for 2,732 distinct URLs, in 192 complete laps of the same list. Every
number on the job page was healthy throughout. `services/scope.py` fixes the
cause it turned out to have; this is the check that would have named it in the
first hour whatever the cause had been.

Answered over a **window of recent rows** rather than the whole capture, for
two reasons. A `COUNT(DISTINCT url)` across a 400,000-row capture is a sort
nobody should pay for on a page that polls. And the whole-capture ratio is
diluted by the healthy start: a crawl that begins looping on its second lap
reads 1.5x over its lifetime and 1.9x over the last few thousand rows, so the
window notices sooner and keeps noticing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cairn.db.models import CaptureUrl

# Rows to look back over. Large enough to span several laps of a site big
# enough to be worth crawling, small enough that the query is cheap while a
# crawl is running.
WINDOW = 20_000

# Above this, a crawl is fetching the same things rather than finding new ones.
# Not 2.0: two URLs that map to one file on disk legitimately cost one extra
# fetch each — measured at exactly 2.0x on 6, 30 and 90 pages, flat — and a
# threshold that fires on a bounded, harmless duplication is a threshold people
# switch off. The capture this was written for sat at 75x.
LOOP_RATIO = 3.0

# Below this there is not enough evidence to say anything. A crawl of forty
# pages that fetched a handful twice is not looping.
MIN_ROWS = 500

EXAMPLES = 5


@dataclass(slots=True)
class Repetition:
    """How much of the recent crawl was ground already covered."""

    checked: int
    distinct: int
    ratio: float
    looping: bool
    # The URLs doing it, which is what makes the number actionable — in the
    # capture this came from, every one of them had a twin under the other
    # scheme, and that was the whole diagnosis.
    worst: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "distinct": self.distinct,
            "ratio": self.ratio,
            "looping": self.looping,
            "worst": self.worst,
        }


def repetition(session: Session, capture_id: int, *, window: int = WINDOW) -> Repetition:
    """How many of the last `window` fetches were of URLs already fetched."""
    recent = (
        select(CaptureUrl.url)
        .where(CaptureUrl.capture_id == capture_id)
        # Newest first: the question is what the crawl is doing *now*, not what
        # it did on the way here.
        .order_by(CaptureUrl.id.desc())
        .limit(window)
        .subquery()
    )
    rows = session.execute(
        select(recent.c.url, func.count().label("n"))
        .group_by(recent.c.url)
        .order_by(func.count().desc())
    ).all()

    checked = sum(int(n) for _url, n in rows)
    distinct = len(rows)
    if distinct == 0:
        return Repetition(checked=0, distinct=0, ratio=0.0, looping=False)

    ratio = checked / distinct
    return Repetition(
        checked=checked,
        distinct=distinct,
        ratio=round(ratio, 1),
        # Both conditions, or a crawl reports itself broken in its first
        # seconds because it fetched the home page and its favicon twice.
        looping=checked >= MIN_ROWS and ratio >= LOOP_RATIO,
        worst=[{"url": url, "count": int(n)} for url, n in rows[:EXAMPLES] if int(n) > 1],
    )
