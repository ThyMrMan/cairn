"""Retention: deciding which captures may be deleted, and refusing most of them.

Naive retention deletes exactly the captures that justify the archive. "Keep
the last three" throws away the only copy of a post the author removed in
2027, and the archive's whole purpose with it — so this is written as a set of
protections with a rule attached, rather than a rule with exceptions.

**Four protections, and the fourth is not obvious.**

`first` — the first capture of a site is never pruned. It is the only one
whose content predates every edit.

`newest` — the most recent N full captures, and one per month beyond that.

`last-copy` — a capture holding a URL that no later capture holds. This is the
clause the whole feature exists for: content that is gone from the live web
survives only here. Note the direction — a capture is protected for being the
*last* copy, not for holding old versions of pages that still exist, because
"an older version of a page that is still there" is precisely what retention
is for discarding.

`dedup-source` — a capture that a later capture's revisit records resolve
into. **Measured, because the failure is silent:** an incremental capture
deduplicated with `--warc-dedup` writes a revisit record — a pointer with no
payload. Delete the capture it points at and pywb answers **503** for a page
whose own capture directory is still entirely present, whose WARC still passes
its checksum, and whose index entry is still there. Without this clause, "keep
the last three captures" can destroy the three it keeps.

Nothing here deletes anything by itself. `plan()` returns what would go and
why each survivor survived; `apply_plan()` is a separate call, and the UI
shows the dry run first.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, Site
from cairn.db.types import to_iso
from cairn.logging import get_logger
from cairn.services import replay, storage

log = get_logger(__name__)

POLICY_SETTING = "retention.captures"

# Off by default, and it stays off until somebody chooses otherwise. An
# archiver that silently deletes archives is not one anybody should trust,
# whatever its defaults are.
DEFAULT_POLICY: dict[str, Any] = {
    "enabled": False,
    # Keep every capture newer than this many.
    "keep_last": 3,
    # Beyond that, keep the newest capture in each calendar month.
    "keep_monthly": 12,
    # Never prune anything younger than this, whatever the counts say.
    "min_age_days": 30,
}

PROTECTIONS = ("first", "newest", "monthly", "min-age", "last-copy", "dedup-source", "running")


class RetentionError(RuntimeError):
    """The plan could not be built or applied."""


@dataclass(slots=True)
class Decision:
    capture_id: int
    dir_name: str
    started_at: datetime
    size_bytes: int
    keep: bool
    reason: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "dir_name": self.dir_name,
            "started_at": to_iso(self.started_at),
            "size_bytes": self.size_bytes,
            "keep": self.keep,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(slots=True)
class Plan:
    site_id: int
    site_title: str
    policy: dict[str, Any]
    decisions: list[Decision] = field(default_factory=list)

    @property
    def prunable(self) -> list[Decision]:
        return [d for d in self.decisions if not d.keep]

    @property
    def freed_bytes(self) -> int:
        return sum(d.size_bytes for d in self.prunable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "site_title": self.site_title,
            "policy": self.policy,
            "captures": [d.to_dict() for d in self.decisions],
            "prunable": len(self.prunable),
            "freed_bytes": self.freed_bytes,
        }


def policy_for(session: Session, site: Site) -> dict[str, Any]:
    """The site's policy, falling back to the instance default."""
    from cairn.services import settings_store

    instance: dict[str, Any] = settings_store.get(session, POLICY_SETTING, {}) or {}
    base = dict(DEFAULT_POLICY)
    base.update(instance)
    override = (site.scope_settings or {}).get("retention")
    if isinstance(override, dict):
        base.update(override)
    return base


def plan(session: Session, settings: Settings, site: Site) -> Plan:
    """What retention would do to this site, and why.

    Always computed in full, even when the policy is off — the dry run is how
    somebody decides whether to switch it on, so it has to work before it is.
    """
    policy = policy_for(session, site)
    captures = list(
        session.scalars(
            select(Capture)
            .where(Capture.site_id == site.id)
            .order_by(Capture.started_at.asc(), Capture.id.asc())
        ).all()
    )
    result = Plan(site_id=site.id, site_title=site.title, policy=policy)
    if not captures:
        return result

    keep_last = max(0, int(policy.get("keep_last") or 0))
    keep_monthly = max(0, int(policy.get("keep_monthly") or 0))
    min_age_days = max(0, int(policy.get("min_age_days") or 0))

    protected: dict[int, tuple[str, str]] = {}

    def protect(capture_id: int, reason: str, detail: str = "") -> None:
        protected.setdefault(capture_id, (reason, detail))

    # ── the first capture ────────────────────────────────────────────────
    protect(captures[0].id, "first", "the first capture of a site is never pruned")

    # ── anything still running or unfinished ─────────────────────────────
    for capture in captures:
        if capture.status not in ("ok", "partial"):
            protect(capture.id, "running", f"status is {capture.status}")

    # ── the newest N, then one per month ─────────────────────────────────
    newest_first = list(reversed(captures))
    for capture in newest_first[:keep_last]:
        protect(capture.id, "newest", f"within the newest {keep_last}")

    months: dict[str, int] = {}
    for capture in newest_first[keep_last:]:
        month = capture.started_at.strftime("%Y-%m")
        if month not in months and len(months) < keep_monthly:
            months[month] = capture.id
            protect(capture.id, "monthly", f"the newest capture in {month}")

    # ── too young to consider ────────────────────────────────────────────
    if min_age_days:
        from cairn.db.types import utcnow

        cutoff = utcnow()
        for capture in captures:
            age = (cutoff - capture.started_at).days
            if age < min_age_days:
                protect(capture.id, "min-age", f"{age} day(s) old, minimum is {min_age_days}")

    # ── the two that read the archive ────────────────────────────────────
    for capture_id, urls in _last_copies(session, captures).items():
        protect(
            capture_id,
            "last-copy",
            f"holds {len(urls)} URL(s) no later capture has, e.g. {sorted(urls)[0]}",
        )

    for capture_id, dependents in _dedup_sources(settings, site, captures).items():
        names = sorted(dependents)
        protect(
            capture_id,
            "dedup-source",
            f"{names[0]}{'' if len(names) == 1 else f' and {len(names) - 1} more'} "
            "deduplicated against it, and would replay 503 without it",
        )

    sizes = _sizes(settings, site, captures)
    for capture in captures:
        reason, detail = protected.get(capture.id, ("", ""))
        result.decisions.append(
            Decision(
                capture_id=capture.id,
                dir_name=capture.dir_name,
                started_at=capture.started_at,
                size_bytes=sizes.get(capture.id, capture.bytes_written or 0),
                keep=bool(reason),
                reason=reason or "prunable",
                detail=detail,
            )
        )
    return result


def _last_copies(session: Session, captures: list[Capture]) -> dict[int, set[str]]:
    """Captures holding a URL that no later capture holds.

    Walked newest-first with a running set, so a URL present in every capture
    protects only the newest one that has it — which is the point. Protecting
    all of them would mean one deleted post pins the entire history of a site
    forever.
    """
    from cairn.db.models import CaptureUrl

    by_capture: dict[int, set[str]] = defaultdict(set)
    rows = session.execute(
        select(CaptureUrl.capture_id, CaptureUrl.url).where(
            CaptureUrl.capture_id.in_([c.id for c in captures]),
            CaptureUrl.error.is_(None),
        )
    ).all()
    for capture_id, url in rows:
        by_capture[int(capture_id)].add(str(url))

    seen: set[str] = set()
    out: dict[int, set[str]] = {}
    for capture in reversed(captures):
        urls = by_capture.get(capture.id, set())
        only_here = urls - seen
        if only_here and seen:
            # `seen` empty means this is the newest capture, which is
            # protected by other rules and would otherwise report every URL it
            # has as unique to it.
            out[capture.id] = only_here
        seen |= urls
    return out


def _dedup_sources(settings: Settings, site: Site, captures: list[Capture]) -> dict[int, set[str]]:
    """Captures that later revisit records resolve into.

    Read from the CDXJ rather than from `capture_urls`, because the index is
    what replay itself resolves against and it records the digest on every
    line — including the revisit lines the engine reconstructs from its crawl
    log, which carry no digest in the database.
    """
    by_dir = {c.dir_name: c.id for c in captures}
    order = {c.dir_name: n for n, c in enumerate(captures)}

    responses: dict[tuple[str, str], list[str]] = defaultdict(list)
    revisits: list[tuple[str, str, str]] = []  # (capture_dir, urlkey, digest)

    for record in replay.index_records(settings, site.archive_path):
        capture_dir = _capture_of(record.filename)
        if capture_dir not in by_dir:
            continue
        key = (record.urlkey, record.digest or "")
        if record.mime and "revisit" in record.mime.lower():
            revisits.append((capture_dir, record.urlkey, record.digest or ""))
        else:
            responses[key].append(capture_dir)

    out: dict[int, set[str]] = defaultdict(set)
    for capture_dir, urlkey, digest in revisits:
        holders = responses.get((urlkey, digest)) or []
        # Only earlier captures can be the source; a revisit never points
        # forwards.
        earlier = [d for d in holders if order.get(d, 0) < order.get(capture_dir, 0)]
        if not earlier:
            continue
        # If several captures still hold the payload, none of them is
        # individually load-bearing — but pruning them all would be, so the
        # oldest is pinned and the rest stay prunable.
        out[by_dir[earlier[0]]].add(capture_dir)
    return dict(out)


def _capture_of(filename: str) -> str:
    parts = filename.split("/")
    return parts[1] if len(parts) > 2 and parts[0] == storage.CAPTURES_DIR else ""


def _sizes(settings: Settings, site: Site, captures: list[Capture]) -> dict[int, int]:
    root = storage.site_dir(settings, site.archive_path) / storage.CAPTURES_DIR
    sizes: dict[int, int] = {}
    for capture in captures:
        directory = root / capture.dir_name
        sizes[capture.id] = (
            storage.directory_size(directory) if directory.is_dir() else capture.bytes_written or 0
        )
    return sizes


# ── carrying it out ──────────────────────────────────────────────────────


@dataclass(slots=True)
class PruneResult:
    pruned: list[str] = field(default_factory=list)
    freed_bytes: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pruned": self.pruned,
            "freed_bytes": self.freed_bytes,
            "errors": self.errors,
        }


def apply_plan(session: Session, settings: Settings, site: Site, plan_: Plan) -> PruneResult:
    """Delete what the plan says is prunable. Nothing about this is reversible.

    The plan is recomputed rather than trusted: it may have been produced
    minutes ago in a browser tab, and a capture that has since become the last
    copy of something must not be deleted because an old plan said it could be.
    """
    import shutil

    from cairn.services import search, textextract

    fresh = plan(session, settings, site)
    allowed = {d.capture_id for d in fresh.prunable}
    asked = {d.capture_id for d in plan_.prunable}
    result = PruneResult()

    for capture_id in sorted(asked):
        if capture_id not in allowed:
            result.errors.append(
                f"capture {capture_id} is protected now even though the plan said otherwise; "
                "it was not deleted"
            )
            continue
        capture = session.get(Capture, capture_id)
        if capture is None:  # pragma: no cover — deleted underneath us
            continue

        directory = (
            storage.site_dir(settings, site.archive_path) / storage.CAPTURES_DIR / capture.dir_name
        )
        size = storage.directory_size(directory) if directory.is_dir() else 0
        search.drop_capture(session, capture.id)
        textextract.remove_capture_text(settings, site.archive_path, capture.dir_name)
        session.delete(capture)
        session.flush()
        if directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)
        result.pruned.append(capture.dir_name)
        result.freed_bytes += size

    if result.pruned:
        # The index still names the WARCs that just went, and a stale index is
        # replay answering 503 for pages that are still there.
        replay.build_index(settings, site.archive_path)
        site.size_bytes = storage.directory_size(storage.site_dir(settings, site.archive_path))
        session.flush()
        log.info(
            "retention pruned captures",
            extra={"site": site.id, "count": len(result.pruned), "freed": result.freed_bytes},
        )
    return result


def due_sites(session: Session, settings: Settings) -> list[int]:
    """Sites whose policy is on and which have something to prune.

    `policy_for` has already folded the instance default into each site's
    policy, so there is one place that decides whether retention is on and it
    is the same one the plan uses.
    """
    out: list[int] = []
    for site in session.scalars(select(Site).where(Site.deleted_at.is_(None))).all():
        if not policy_for(session, site).get("enabled"):
            continue
        if plan(session, settings, site).prunable:
            out.append(site.id)
    return out
