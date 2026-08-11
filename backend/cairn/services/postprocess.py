"""Post-capture processing (docs/05).

The addon-facing version of this is M7: a manifest, an order, a subprocess.
What ships here is the same *shape* — an ordered chain of named steps, each
marked required or not, each able to fail without taking the capture with it —
implemented in-process because the three built-ins need no isolation and a
subprocess contract nobody has written a second implementation of is a
contract that will be wrong.

Order matters and matches docs/05: checksums before stats (so sizes are known
and verified), stats before the manifest is written (so it records the truth),
and the asset audit last because it is advisory.

Failure policy: a `required` step that fails downgrades the capture, because
a capture with no verified checksums is not one you can trust years later. An
optional step that fails is logged and skipped.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, CaptureUrl, Site
from cairn.db.types import utcnow
from cairn.logging import get_logger
from cairn.services import htmlrefs, storage
from cairn.services.scope import Scope, ScopeError, build_reject_patterns

log = get_logger(__name__)

CHECKSUM_CHUNK = 1024 * 1024
LAZY_ATTRIBUTES = (b"data-src", b"data-srcset", b"data-original", b"data-lazy-src")


@dataclass(slots=True)
class Context:
    session: Session
    settings: Settings
    capture: Capture
    site: Site
    output_dir: Path
    tool_version: str | None
    stats: dict[str, Any]
    scope: dict[str, Any]
    seeds: list[str]
    seed_source: dict[str, int]
    artifacts: list[dict[str, Any]]
    warnings: list[str]


@dataclass(slots=True)
class Step:
    id: str
    order: int
    required: bool
    run: Callable[[Context], None]


# ── steps ────────────────────────────────────────────────────────────────


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHECKSUM_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def step_checksum(ctx: Context) -> None:
    """SHA-256 every artifact and confirm it is actually on disk.

    The weekly integrity job compares against these. Recording a checksum the
    engine claimed rather than one computed here would make that job verify
    the engine's memory instead of the archive.
    """
    verified: list[dict[str, Any]] = []
    for artifact in ctx.artifacts:
        try:
            path = storage.resolve_within(ctx.output_dir, artifact["name"])
        except storage.StoragePathError:
            ctx.warnings.append(f"artifact outside the capture directory: {artifact['name']}")
            continue
        if not path.is_file():
            ctx.warnings.append(f"artifact is missing from disk: {artifact['name']}")
            continue
        verified.append(
            {
                "name": artifact["name"],
                "kind": artifact.get("kind", "file"),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    ctx.artifacts[:] = verified
    ctx.capture.warc_files = verified


def step_stats(ctx: Context) -> None:
    """Roll counts and sizes onto the capture and the site."""
    session, capture, site = ctx.session, ctx.capture, ctx.site

    counted = session.scalar(
        select(func.count(CaptureUrl.id)).where(CaptureUrl.capture_id == capture.id)
    )
    errors = session.scalar(
        select(func.count(CaptureUrl.id)).where(
            CaptureUrl.capture_id == capture.id,
            (CaptureUrl.status_code >= 400) | (CaptureUrl.error.isnot(None)),
        )
    )
    if counted:
        capture.url_count = counted
    if errors is not None:
        capture.error_count = errors
    capture.bytes_written = sum(int(a.get("size") or 0) for a in ctx.artifacts)

    site_root = storage.site_dir(ctx.settings, site.archive_path)
    site.size_bytes = storage.directory_size(site_root)
    site.url_count = (
        session.scalar(
            select(func.count(func.distinct(CaptureUrl.url)))
            .select_from(CaptureUrl)
            .join(Capture, Capture.id == CaptureUrl.capture_id)
            .where(Capture.site_id == site.id)
        )
        or 0
    )
    site.updated_at = utcnow()

    ctx.stats.update(
        {
            "urls": capture.url_count,
            "errors": capture.error_count,
            "bytes": capture.bytes_written,
            "site_bytes": site.size_bytes,
        }
    )


def step_manifest(ctx: Context) -> None:
    """Write `manifest.json` — the capture's half of the rebuildable pair."""
    payload = storage.build_manifest(
        capture_id=ctx.capture.id,
        site_slug=ctx.site.slug,
        kind=ctx.capture.kind,
        engine_id=ctx.capture.engine_id,
        engine_version=ctx.capture.engine_version or "",
        tool_version=ctx.tool_version,
        started_at=ctx.capture.started_at,
        finished_at=ctx.capture.finished_at,
        status=ctx.capture.status,
        seeds=ctx.seeds,
        seed_source=ctx.seed_source,
        scope=ctx.scope,
        stats=ctx.stats,
        warc_files=ctx.artifacts,
    )
    storage.write_json(ctx.output_dir / storage.MANIFEST_FILE, payload)


def step_asset_audit(ctx: Context) -> None:
    """Report assets a page referenced that the capture does not contain.

    This is the safety net for the scope translation. No regex over URLs can
    tell an extension-less image from an extension-less page, so an
    assets-only host can silently lose images; wget also cannot see
    lazy-loaded ones at all. Rather than leaving both to be discovered during
    replay months later, compare what the archived HTML asks for against what
    was actually fetched, and say so now.
    """
    try:
        from warcio.archiveiterator import ArchiveIterator
    except ImportError:  # pragma: no cover — optional at runtime
        return

    warcs = sorted((ctx.output_dir / storage.WARC_DIR).glob("*.warc.gz"))
    if not warcs:
        return

    captured = set(
        ctx.session.scalars(
            select(CaptureUrl.url).where(CaptureUrl.capture_id == ctx.capture.id)
        ).all()
    )

    referenced: set[str] = set()
    lazy_hits = 0
    scanned = 0
    for warc in warcs:
        try:
            with open(warc, "rb") as fh:
                for record in ArchiveIterator(fh):
                    if record.rec_type != "response":
                        continue
                    if not record.http_headers:
                        continue
                    # Error pages are not pages. `--content-on-error` archives
                    # the body of every 404, and a site's 404 template
                    # references its own logo and stylesheet — which are then
                    # reported as assets the capture is missing, from a page
                    # nobody asked for. It also made the page count nonsense:
                    # 4 real pages and 12 mangled requests read as "16 pages".
                    if not _is_success(record.http_headers.get_statuscode()):
                        continue
                    ctype = record.http_headers.get_header("Content-Type", "") or ""
                    if "html" not in ctype.lower():
                        continue
                    body = record.content_stream().read(512 * 1024)
                    scanned += 1
                    lazy_hits += sum(body.count(attr) for attr in LAZY_ATTRIBUTES)
                    base = record.rec_headers.get_header("WARC-Target-URI") or ""
                    referenced |= _referenced_assets(body, base)
        except Exception as exc:
            # An unreadable or truncated WARC downgrades the audit to a
            # warning; it must never turn a completed capture into a failure.
            log.warning(
                "asset audit could not read a WARC",
                extra={"warc": warc.name, "err": str(exc)},
            )
            continue

    missing = sorted(u for u in referenced if u not in captured)
    absent, excluded = _partition_missing(ctx, missing)
    if absent:
        ctx.warnings.append(
            f"{len(absent)} referenced asset(s) were not captured (e.g. {', '.join(absent[:3])})."
        )
    if excluded:
        hosts = sorted({htmlrefs.host_of(u) for u in excluded if htmlrefs.host_of(u)})
        ctx.warnings.append(
            f"{len(excluded)} referenced asset(s) are outside this site's scope, so they "
            f"were not fetched: {', '.join(hosts[:4])}. That is what the domain picker is "
            "currently set to, not a failure — turn the host on if you want them."
        )
    if lazy_hits:
        ctx.warnings.append(
            f"{lazy_hits} lazy-loaded image reference(s) found in {scanned} page(s). "
            "wget cannot execute JavaScript, so those images are not in this archive."
        )

    mangled = _css_escaped_requests(ctx)
    if mangled:
        hosts = sorted({h for h in (_escaped_target_host(u) for u in mangled) if h})
        ctx.warnings.append(
            f"{len(mangled)} request(s) failed because the URL was CSS-escaped. "
            "wget does not decode CSS escape sequences, so an absolute URL written "
            r"as url(https\:\/\/host\/x.png) — which Blogger skins use for theme "
            "images — looks relative to it and is requested against this site "
            f"instead{': ' + ', '.join(hosts[:3]) if hosts else ''}. "
            "The page text is unaffected; the asset itself is not in this archive."
        )

    ctx.stats["referenced_assets"] = len(referenced)
    # `missing_assets` counts only what the scope permitted and the crawl
    # still did not get. That is the number worth acting on; the deliberate
    # exclusions are counted separately so neither one dilutes the other.
    ctx.stats["missing_assets"] = len(absent)
    ctx.stats["excluded_assets"] = len(excluded)
    ctx.stats["lazy_image_hints"] = lazy_hits
    ctx.stats["css_escaped_failures"] = len(mangled)


# Extraction lives in htmlrefs so discovery and this audit cannot drift: one
# would then offer hosts the other reports as missing.
_referenced_assets = htmlrefs.referenced_assets
_unescape_css = htmlrefs.unescape_css


def _is_success(status: str | None) -> bool:
    return bool(status) and str(status).startswith("2")


def _partition_missing(ctx: Context, missing: list[str]) -> tuple[list[str], list[str]]:
    """Split what a page asked for and did not get into two unlike halves.

    "In scope and still absent" is a problem. "Out of scope" is a setting.
    Reporting them as one number teaches people to ignore the report: on a
    Blogger blog the second list is never empty, because the preset
    deliberately drops the owner's admin-bar CSS and a comment iframe that
    cannot work offline. Every capture would open with "3 referenced assets
    were not captured" forever, and the one time it meant something nobody
    would be reading it any more.

    A scope that will not parse, or that carries no host rules at all, is not
    worth guessing about — everything is reported as absent. Explaining a gap
    away needs positive evidence that somebody chose it; absence of evidence
    is the one direction this must not fail in.
    """
    try:
        scope = Scope.from_dict(ctx.scope)
        rejects = [re.compile(p) for p in build_reject_patterns(scope)]
    except (ScopeError, re.error, TypeError, ValueError):
        return list(missing), []

    asset_hosts = {rule.host for rule in scope.hosts if rule.fetch_assets}
    if not asset_hosts:
        return list(missing), []
    excluded_hosts = set(scope.exclude_hosts)

    absent: list[str] = []
    excluded: list[str] = []
    for url in missing:
        host = htmlrefs.host_of(url)
        out_of_scope = host not in asset_hosts or host in excluded_hosts
        # A reject pattern is just as deliberate as an unticked checkbox —
        # including the generated one that fences an assets-only host.
        if out_of_scope or any(pattern.search(url) for pattern in rejects):
            excluded.append(url)
        else:
            absent.append(url)
    return absent, excluded


def _css_escaped_requests(ctx: Context) -> list[str]:
    """URLs the crawl requested that are mangled CSS escapes.

    These show up as 404s against the site's own host with a backslash in the
    path — percent-encoded, so `%5C`. Recognising them turns a pair of
    confusing log lines into an explanation.

    `autoescape` is not optional here: without it the `%` is passed through to
    SQL as a LIKE wildcard, so the filter degrades to "contains 5C" and
    matches ordinary URLs that happen to have those two characters in a
    cache-busting token.
    """
    rows = ctx.session.scalars(
        select(CaptureUrl.url).where(
            CaptureUrl.capture_id == ctx.capture.id,
            CaptureUrl.url.contains("%5C", autoescape=True)
            | CaptureUrl.url.contains("%5c", autoescape=True)
            | CaptureUrl.url.contains("\\", autoescape=True),
        )
    ).all()
    return list(rows)


def _escaped_target_host(url: str) -> str | None:
    """The host the mangled URL was *meant* to reach."""
    from urllib.parse import unquote, urlsplit

    decoded = _unescape_css(unquote(url))
    match = re.search(r"https?://([^/\s\\]+)", decoded[decoded.find("//") + 2 :])
    if match:
        return match.group(1)
    # The scheme survives only once; fall back to the last embedded host-like run.
    tail = decoded.split("://")
    if len(tail) > 2:
        return urlsplit("//" + tail[-1]).netloc or None
    return None


CHAIN: list[Step] = [
    Step("checksum", 20, True, step_checksum),
    Step("stats", 30, True, step_stats),
    Step("manifest", 35, True, step_manifest),
    Step("asset-audit", 60, False, step_asset_audit),
]


def run_chain(
    session: Session,
    settings: Settings,
    *,
    capture: Capture,
    site: Site,
    output_dir: Path,
    tool_version: str | None,
    stats: dict[str, Any],
    scope: dict[str, Any],
    seeds: list[str],
    seed_source: dict[str, int] | None = None,
    warnings: list[str] | None = None,
) -> Context:
    ctx = Context(
        session=session,
        settings=settings,
        capture=capture,
        site=site,
        output_dir=output_dir,
        tool_version=tool_version,
        stats=dict(stats),
        scope=scope,
        seeds=seeds,
        seed_source=seed_source or {"manual": len(seeds)},
        artifacts=list(capture.warc_files or []),
        # Seeded with whatever the supervisor already knew was wrong before
        # the crawl started, so one report covers the whole capture.
        warnings=list(warnings or []),
    )

    for step in sorted(CHAIN, key=lambda s: s.order):
        try:
            step.run(ctx)
        except Exception as exc:
            log.exception("post-processor failed", extra={"step": step.id, "capture": capture.id})
            ctx.warnings.append(f"{step.id} failed: {exc}")
            if step.required and capture.status == "ok":
                capture.status = "partial"

    if ctx.warnings:
        # The manifest is already written by then; keep the warnings where the
        # UI reads them rather than only in the log.
        capture.warc_files = ctx.artifacts
        stats_with_warnings = dict(ctx.stats)
        stats_with_warnings["warnings"] = ctx.warnings
        ctx.stats = stats_with_warnings
        step_manifest(ctx)

    session.flush()
    return ctx
