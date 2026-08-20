"""The skip patterns that apply to every site, not just one.

A site's own reject patterns live in `scope_patterns`, keyed by `site_id`.
That is right for a rule about one blog's pagination and wrong for a rule
about the web: `[?&]utm_source=` is junk everywhere, and typing it into each
domain picker in turn means a site added next month silently does not have it.

So this list is stored once, in `settings`, and merged in at the moment a
scope is resolved rather than copied into any site. Three consequences, and
all three are the point:

  - **It is retroactive.** Adding a pattern changes what every site's next
    capture fetches, without visiting any of them.
  - **Removing it removes it everywhere.** If the list were copied into sites
    on creation, "remove" would only mean "stop giving it to new ones", and
    the rule would live on in every site that already had it with nothing to
    say it came from here.
  - **The two lists stay distinguishable.** The domain picker shows a site's
    own patterns and never writes these back into them, so a scope saved
    today does not quietly absorb the global list and outlive it.

Patterns are compiled here, on write. A bad regex in a *site's* list breaks
that site; a bad regex here breaks every capture on the instance at once, so
it is refused at the point somebody can still see what they typed.

A per-site escape hatch exists — see `sites.global_reject_exceptions`. Without
one this list would be a one-way door, which is the same trap the preset
`retired_patterns` field was added to get out of.
"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from cairn.services import settings_store

SETTING = "crawl.global_reject_patterns"

# Same ceiling the per-site list carries in `ScopeModel`. The regex is joined
# into one alternation and handed to a subprocess argument; a list nobody
# could have meant is a command line nothing can run.
MAX_PATTERNS = 200
MAX_LENGTH = 1024


class SkipListError(ValueError):
    """A pattern is unusable, or there are too many of them."""


def load(session: Session) -> list[str]:
    raw: object = settings_store.get(session, SETTING, [])
    if not isinstance(raw, list):  # hand-edited, or an older shape
        return []
    return [str(p) for p in raw if str(p).strip()]


def normalize(patterns: list[str]) -> list[str]:
    """Trim, de-duplicate and compile. Order is the order given."""
    cleaned: list[str] = []
    for raw in patterns:
        pattern = str(raw).strip()
        if not pattern or pattern in cleaned:
            continue
        if len(pattern) > MAX_LENGTH:
            raise SkipListError(f"pattern is longer than {MAX_LENGTH} characters")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise SkipListError(f"{pattern!r} is not a valid regular expression: {exc}") from exc
        cleaned.append(pattern)
    if len(cleaned) > MAX_PATTERNS:
        raise SkipListError(f"a skip list holds at most {MAX_PATTERNS} patterns")
    return cleaned


def save(session: Session, patterns: list[str]) -> list[str]:
    cleaned = normalize(patterns)
    settings_store.put(session, SETTING, cleaned)
    return cleaned


def add(session: Session, pattern: str) -> list[str]:
    """Append one pattern. Adding one that is already there is not an error."""
    return save(session, [*load(session), pattern])


def remove(session: Session, pattern: str) -> list[str]:
    """Drop one pattern. Removing one that is not there is not an error.

    Both halves are deliberate: these are called from a list somebody may have
    open in two tabs, and the second click should agree with the first rather
    than raise.
    """
    wanted = pattern.strip()
    return save(session, [p for p in load(session) if p != wanted])
