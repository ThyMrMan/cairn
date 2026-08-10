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
from cairn.services import storage

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
        seed_source={"manual": len(ctx.seeds)},
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
                    ctype = (
                        record.http_headers.get_header("Content-Type", "")
                        if (record.http_headers)
                        else ""
                    )
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
    if missing:
        ctx.warnings.append(
            f"{len(missing)} referenced asset(s) were not captured (e.g. {', '.join(missing[:3])})."
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
    ctx.stats["missing_assets"] = len(missing)
    ctx.stats["lazy_image_hints"] = lazy_hits
    ctx.stats["css_escaped_failures"] = len(mangled)


_TAG_REFS = re.compile(
    r"""<(?:img|script|source|video|audio|embed)\b[^>]*?\bsrc\s*=\s*["']([^"'>]+)["']"""
    r"""|<link\b[^>]*?\bhref\s*=\s*["']([^"'>]+)["'][^>]*?\brel\s*=\s*["']?stylesheet"""
    r"""|<link\b[^>]*?\brel\s*=\s*["']?stylesheet[^>]*?\bhref\s*=\s*["']([^"'>]+)["']""",
    re.IGNORECASE,
)
_CSS_URLS = re.compile(r"""url\(\s*['"]?(.*?)['"]?\s*\)""", re.IGNORECASE | re.DOTALL)
# A CSS escape: a backslash followed by one non-hex-digit character. Enough for
# the \: and \/ that appear in Blogger skins; full CSS unescaping (hex escapes,
# line continuations) is not needed to recognise a mangled URL.
_CSS_ESCAPE = re.compile(r"\\([^0-9A-Fa-f\s])")


def _unescape_css(value: str) -> str:
    r"""Decode the backslash escapes a browser would resolve.

    Blogger skins write theme images as `url(https\:\/\/host\/image?id=…)`.
    A browser unescapes that to an absolute URL; wget does not, so it treats
    the string as relative and requests it against the blog. Decoding here is
    what lets the audit recognise the real target and name it.
    """
    return _CSS_ESCAPE.sub(r"\1", value)


def _referenced_assets(body: bytes, base_url: str) -> set[str]:
    """Absolute URLs of subresources referenced by an HTML body.

    Covers tag attributes and CSS `url(...)` in `<style>` blocks and inline
    `style=` attributes. The CSS half matters because it is where the
    references wget mishandles actually live.
    """
    from urllib.parse import urljoin

    text = body.decode("utf-8", errors="replace")
    found: set[str] = set()

    raws = [next((g for g in m.groups() if g), None) for m in _TAG_REFS.finditer(text)]
    raws += [_unescape_css(m.group(1)) for m in _CSS_URLS.finditer(text)]

    for raw in raws:
        if not raw:
            continue
        candidate = _unescape_css(raw.strip())
        if candidate.startswith(("data:", "javascript:", "#", "mailto:", "about:")):
            continue
        absolute = urljoin(base_url, candidate)
        if absolute.startswith(("http://", "https://")):
            found.add(absolute.split("#")[0])
    return found


def _css_escaped_requests(ctx: Context) -> list[str]:
    """URLs the crawl requested that are mangled CSS escapes.

    These show up as 404s against the site's own host with a backslash in the
    path — percent-encoded, so `%5C`. Recognising them turns a pair of
    confusing log lines into an explanation.
    """
    rows = ctx.session.scalars(
        select(CaptureUrl.url).where(
            CaptureUrl.capture_id == ctx.capture.id,
            CaptureUrl.url.contains("%5C") | CaptureUrl.url.contains("\\"),
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
        artifacts=list(capture.warc_files or []),
        warnings=[],
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
