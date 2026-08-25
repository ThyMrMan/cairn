"""What a skip pattern would actually skip.

A reject pattern is written blind. It compiles or it does not, and that is the
only feedback there has ever been — so a pattern that is *valid* and matches
*nothing* saves silently and looks exactly like one that works. The way that
gets discovered is counting a crawl an hour later and finding the URLs still
there.

The archive already knows the answer. `capture_urls` holds every URL a capture
fetched, so "would this pattern have done anything to the last crawl?" is a
question with a real number behind it rather than a guess.

Deliberately answered against **what was fetched**, not against what would be
fetched next time. Those differ — a pattern that fires stops URLs being
discovered at all, so the next capture's list is smaller than this count
predicts. The count is a floor and a sanity check, which is what "did I write
this right?" needs; a simulation of the next crawl is a different and much
more expensive question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.db.models import Capture, CaptureUrl

# The sample is bounded because this runs while somebody waits for a panel to
# draw, and an archive can hold millions of URL rows. Every pattern is tested
# against every sampled URL, so the work is patterns times URLs.
MAX_URLS = 20_000
# How far back to reach when no site is named. Recent rather than
# representative on purpose: the question being asked is about a rule somebody
# is writing now, and a capture from two years ago answers a different one.
MAX_CAPTURES = 12
EXAMPLES = 3


@dataclass(slots=True)
class Hit:
    pattern: str
    count: int
    examples: list[str]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern,
            "count": self.count,
            "examples": self.examples,
            "error": self.error,
        }


def _sample(session: Session, site_id: int | None) -> tuple[list[str], int]:
    """Recent URLs, newest capture first. Returns the sample and how many
    captures it came from."""
    captures = select(Capture.id).order_by(Capture.id.desc()).limit(MAX_CAPTURES)
    if site_id is not None:
        captures = captures.where(Capture.site_id == site_id)
    ids = list(session.scalars(captures).all())
    if not ids:
        return [], 0

    urls = list(
        session.scalars(
            select(CaptureUrl.url)
            .where(CaptureUrl.capture_id.in_(ids))
            # Newest first, so a truncated sample is the recent end of the
            # archive rather than an arbitrary slice of it.
            .order_by(CaptureUrl.id.desc())
            .limit(MAX_URLS)
        ).all()
    )
    return urls, len(ids)


def check(session: Session, patterns: list[str], *, site_id: int | None = None) -> dict[str, Any]:
    """Count how many recently-fetched URLs each pattern matches."""
    wanted = [p for p in dict.fromkeys(patterns) if p.strip()]
    urls, captures = _sample(session, site_id)

    hits: list[Hit] = []
    for pattern in wanted:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            # Reachable for a site's own patterns, which are only validated
            # when the whole scope is saved. Reported rather than raised: one
            # bad pattern must not cost the counts for the rest.
            hits.append(Hit(pattern, 0, [], f"not a valid regular expression: {exc}"))
            continue
        matched = [url for url in urls if compiled.search(url)]
        hits.append(Hit(pattern, len(matched), matched[:EXAMPLES]))

    return {
        "checked": len(urls),
        "captures": captures,
        "truncated": len(urls) >= MAX_URLS,
        "results": [hit.to_dict() for hit in hits],
    }
