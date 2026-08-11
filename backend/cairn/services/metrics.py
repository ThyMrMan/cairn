"""Prometheus metrics.

Off by default, and unauthenticated when on — because a scraper cannot log in.
That pair is the whole design problem, and it is resolved by deciding what
must never appear here:

**No names.** Not a site title, not a URL, not a host, not a folder, not a tag.
An exporter is scraped by something that stores forever and is often exposed
more widely than the app is, and "which sites does this person archive" is the
most sensitive thing this application knows that is not a credential. So every
series is a count or a duration, and the only labels are fixed vocabularies —
job types, statuses — that give away nothing about the archive's contents.

**A token, if you want one.** `metrics.token` turns the endpoint into
`Authorization: Bearer …`, which is what Prometheus's `bearer_token` does
natively. Empty by default, because a metrics endpoint that leaks nothing is a
reasonable thing to leave open on a LAN, and because a token nobody can
configure in their scrape job is a token that gets switched off.

Written by hand rather than with `prometheus_client`: the exposition format is
a line per series, this exports about thirty of them, and the library's global
registry does not fit an app that can be instantiated twice in one process
(which the tests do on every fixture).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, Feed, Job, PageText, Site
from cairn.logging import get_logger

log = get_logger(__name__)

ENABLED_SETTING = "metrics.enabled"
TOKEN_SETTING = "metrics.token"  # noqa: S105 — a settings key, not a secret

JOB_STATUSES = ("queued", "running", "ok", "failed", "cancelled", "interrupted")
JOB_TYPES = ("capture", "discovery", "mint", "index", "export", "move", "verify", "purge")
CAPTURE_STATUSES = ("ok", "partial", "failed", "cancelled", "interrupted", "running")


@dataclass(slots=True)
class Series:
    name: str
    help: str
    kind: str  # gauge | counter
    samples: list[tuple[dict[str, str], float]]

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]
        for labels, value in self.samples:
            rendered = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
            suffix = f"{{{rendered}}}" if rendered else ""
            lines.append(f"{self.name}{suffix} {_number(value)}")
        return "\n".join(lines)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def enabled(session: Session) -> bool:
    from cairn.services import settings_store

    return bool(settings_store.get(session, ENABLED_SETTING, False))


def token(session: Session) -> str:
    from cairn.services import settings_store

    return str(settings_store.get(session, TOKEN_SETTING, "") or "")


def render(session: Session, settings: Settings) -> str:
    """The whole exposition, as text.

    One pass of small aggregate queries. Nothing here walks the filesystem
    except `statvfs`, because a scrape happens every fifteen seconds and a
    scrape that spins up an array is worse than no metrics.
    """
    return "\n".join(series.render() for series in collect(session, settings)) + "\n"


def collect(session: Session, settings: Settings) -> list[Series]:
    from cairn.build import build_info

    info = build_info()
    out: list[Series] = [
        Series(
            "cairn_build_info",
            "Build information. Always 1; read the labels.",
            "gauge",
            [({"version": info.version, "build": info.build or "source"}, 1)],
        )
    ]

    sites_total = session.scalar(select(func.count(Site.id)).where(Site.deleted_at.is_(None))) or 0
    trashed = session.scalar(select(func.count(Site.id)).where(Site.deleted_at.isnot(None))) or 0
    out.append(
        Series(
            "cairn_sites",
            "Sites, by whether they are in the trash.",
            "gauge",
            [({"state": "active"}, sites_total), ({"state": "trashed"}, trashed)],
        )
    )

    by_status: dict[str, int] = {
        str(status): int(count)
        for status, count in session.execute(
            select(Capture.status, func.count(Capture.id)).group_by(Capture.status)
        ).all()
    }
    out.append(
        Series(
            "cairn_captures",
            "Captures, by status.",
            "gauge",
            [({"status": s}, by_status.get(s, 0)) for s in CAPTURE_STATUSES],
        )
    )

    jobs_by: dict[str, int] = {
        str(status): int(count)
        for status, count in session.execute(
            select(Job.status, func.count(Job.id)).group_by(Job.status)
        ).all()
    }
    out.append(
        Series(
            "cairn_jobs",
            "Jobs, by status.",
            "gauge",
            [({"status": s}, jobs_by.get(s, 0)) for s in JOB_STATUSES],
        )
    )

    queued_types: dict[str, int] = {
        str(kind): int(count)
        for kind, count in session.execute(
            select(Job.type, func.count(Job.id))
            .where(Job.status.in_(("queued", "running")))
            .group_by(Job.type)
        ).all()
    }
    out.append(
        Series(
            "cairn_jobs_pending",
            "Jobs queued or running, by type.",
            "gauge",
            [({"type": t}, queued_types.get(t, 0)) for t in JOB_TYPES],
        )
    )

    out.append(
        Series(
            "cairn_archive_bytes",
            "Bytes across all site directories, as last measured by a capture.",
            "gauge",
            [
                (
                    {},
                    session.scalar(
                        select(func.coalesce(func.sum(Site.size_bytes), 0)).where(
                            Site.deleted_at.is_(None)
                        )
                    )
                    or 0,
                )
            ],
        )
    )
    out.append(
        Series(
            "cairn_archive_urls",
            "Distinct URLs across all sites, as last measured by a capture.",
            "gauge",
            [
                (
                    {},
                    session.scalar(
                        select(func.coalesce(func.sum(Site.url_count), 0)).where(
                            Site.deleted_at.is_(None)
                        )
                    )
                    or 0,
                )
            ],
        )
    )

    feeds_enabled = session.scalar(select(func.count(Feed.id)).where(Feed.enabled.is_(True))) or 0
    feeds_off = session.scalar(select(func.count(Feed.id)).where(Feed.enabled.is_(False))) or 0
    failing = session.scalar(select(func.count(Feed.id)).where(Feed.consecutive_failures > 0)) or 0
    out.append(
        Series(
            "cairn_feeds",
            "Watched feeds, sitemaps and pages.",
            "gauge",
            [
                ({"state": "enabled"}, feeds_enabled),
                ({"state": "disabled"}, feeds_off),
                ({"state": "failing"}, failing),
            ],
        )
    )

    out.append(
        Series(
            "cairn_search_pages",
            "Pages in the full-text index.",
            "gauge",
            [({}, session.scalar(select(func.count(PageText.id))) or 0)],
        )
    )

    out += _disk(settings)
    out += _integrity(session, settings)
    return out


def _disk(settings: Settings) -> list[Series]:
    try:
        usage = shutil.disk_usage(settings.data_dir)
    except OSError:  # pragma: no cover — the volume went away
        return []
    return [
        Series(
            "cairn_disk_bytes",
            "The archive volume, from statvfs.",
            "gauge",
            [({"kind": "free"}, usage.free), ({"kind": "total"}, usage.total)],
        )
    ]


def _integrity(session: Session, settings: Settings) -> list[Series]:
    from cairn.services import integrity

    last = integrity.load(settings)
    if not last:
        return []
    findings = last.get("findings") or []
    out = [
        Series(
            "cairn_integrity_findings",
            "Findings from the most recent verification pass.",
            "gauge",
            [({}, len(findings))],
        ),
        Series(
            "cairn_integrity_files_checked",
            "Files read by the most recent verification pass.",
            "gauge",
            [({}, int(last.get("files") or 0))],
        ),
    ]
    finished = last.get("finished_at")
    if finished:
        from datetime import datetime

        try:
            when = datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
        except ValueError:  # pragma: no cover — hand-edited report
            return out
        out.append(
            Series(
                "cairn_integrity_last_run_timestamp_seconds",
                "When the most recent verification pass finished, as a unix timestamp.",
                "gauge",
                [({}, when.timestamp())],
            )
        )
    return out


def defaults() -> dict[str, Any]:
    return {ENABLED_SETTING: False, TOKEN_SETTING: ""}
