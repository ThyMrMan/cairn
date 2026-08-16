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
from cairn.db.models import Capture, CaptureUrl, EngineRecord, Site
from cairn.db.types import utcnow
from cairn.logging import get_logger
from cairn.services import htmlrefs, interstitial, replay, storage
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


def step_cdxj_index(ctx: Context) -> None:
    """Rebuild the site's replay index across every capture it has.

    Across all captures, not just this one: the index is what gives replay its
    time dimension, so a page captured five times has five versions to switch
    between. Rebuilding the lot is fast and removes any chance of the index
    disagreeing with the archive about a capture that was deleted.

    Not required. A capture whose index failed is still a complete, valid
    archive — the index is derived data, regenerable at any time from the
    Rebuild index action, and failing the capture over it would be a lie
    about what is on disk.
    """
    result = replay.build_index(
        ctx.settings,
        ctx.site.archive_path,
        withhold=replay.withheld_patterns(ctx.session, ctx.site),
    )
    ctx.stats["index_records"] = result.records
    # Declared, never silent. The bytes are in the WARC and replay will not
    # serve them, and a future reader has to be able to tell that apart from
    # "it was never captured" — otherwise the archive is quietly lying about
    # its own contents.
    ctx.stats["index_withheld"] = result.withheld
    if result.withheld:
        ctx.warnings.append(
            f"{result.withheld} archived record(s) match this site's skip patterns and are "
            "kept out of replay. The bytes are still in the WARCs — this only stops them "
            "being served, which is the only lever that works on something the crawler "
            "records anyway, like Blogger's content-warning iframe. Clear the pattern and "
            "rebuild the index to get them back."
        )
    try:
        replay.link_collection(ctx.settings, ctx.site.id, ctx.site.archive_path)
    except replay.ReplayError as exc:
        # Most likely a filesystem without symlink permission — a Windows dev
        # box. The archive is intact; only replay is unavailable.
        ctx.warnings.append(f"the archive is complete, but replay could not be set up: {exc}")


def step_text_extract(ctx: Context) -> None:
    """Extract readable text and put this capture's pages in the search index.

    Not required, and deliberately: an archive whose text could not be
    extracted is still a complete archive, and failing a three-hour capture
    over a search index that `Rebuild search index` regenerates in seconds
    would be the wrong trade every time.
    """
    from cairn.services import search, settings_store, textextract

    if not settings_store.get(ctx.session, search.INDEX_SETTING, True):
        return

    result = textextract.extract_capture(ctx.settings, ctx.site.archive_path, ctx.capture.dir_name)
    if not result.pages:
        return
    indexed = search.index_capture(
        ctx.session, ctx.settings, site=ctx.site, capture=ctx.capture, pages=result.pages
    )
    ctx.stats["text_pages"] = indexed
    ctx.stats["text_words"] = sum(len(p.text.split()) for p in result.pages)
    ctx.stats["text_boilerplate_blocks"] = result.dropped_blocks


def step_thumbnail(ctx: Context) -> None:
    """Photograph the archived front page for the site card.

    Two conditions, and the second is the one that matters. A picture is taken
    when there is none yet, or when the newest archived version of the page it
    would show came from *this* capture. An incremental capture of one new post
    has not changed what the front page looks like, so re-photographing it
    would start a browser after every feed poll to produce the same image.

    Silent when it cannot run. Unlike the media step — which somebody switched
    on for this site and deserves to hear about — a thumbnail is decoration,
    and an install whose replay sidecar is not running would otherwise carry a
    warning on every capture of every site, teaching the reader to skip the
    warnings that mean something. The reason is recorded in the manifest.
    """
    from cairn.services import sites as site_service
    from cairn.services import thumbnail

    if not thumbnail.enabled(ctx.session):
        return

    seeds = site_service.all_seeds(ctx.site)
    record = thumbnail.subject(ctx.settings, ctx.site.archive_path, seeds)
    if record is None:
        return
    if not thumbnail.is_from_capture(record, ctx.capture.dir_name) and thumbnail.exists(
        ctx.settings, ctx.site.archive_path
    ):
        return

    try:
        shot = thumbnail.capture_site(
            ctx.settings,
            site_id=ctx.site.id,
            archive_path=ctx.site.archive_path,
            seeds=seeds,
            record=record,
        )
    except thumbnail.ThumbnailError as exc:
        ctx.stats["thumbnail_skipped"] = str(exc)
        log.info("no thumbnail for this capture", extra={"site": ctx.site.id, "why": str(exc)})
        return
    ctx.stats["thumbnail"] = shot.to_dict()


def step_media(ctx: Context) -> None:
    """Fetch the video an archived post embedded, if this site asked for it.

    Off unless switched on per site, because it is the one step that can turn
    a megabyte capture into a gigabyte one. Everything it refuses — a limit
    reached, a private host, an unsupported embed — is recorded in the stats
    rather than dropped, since "the video is not here" is exactly the thing
    somebody needs to find out now rather than in five years.
    """
    from cairn.services import media

    policy = media.policy_for(ctx.session, ctx.site)
    if not policy.get("enabled"):
        return

    ok, reason = media.available()
    if not ok:
        ctx.warnings.append(f"media download is on for this site but {reason}.")
        return

    urls = _embedded_media(ctx)
    if not urls:
        return

    target = media.media_dir(ctx.settings, ctx.site.archive_path, ctx.capture.dir_name)
    result = media.download(urls, target, policy)

    ctx.stats["media"] = result.to_dict()
    if result.downloaded:
        ctx.stats["media_bytes"] = result.bytes
    refused = [i for i in result.items if i.status != "downloaded"]
    if refused:
        ctx.warnings.append(
            f"{len(refused)} embedded item(s) were not downloaded — e.g. {refused[0].url}: "
            f"{refused[0].reason}"
        )


def _embedded_media(ctx: Context) -> list[str]:
    """Media URLs referenced by this capture's archived HTML."""
    try:
        from warcio.archiveiterator import ArchiveIterator
    except ImportError:  # pragma: no cover — declared in pyproject
        return []
    from cairn.services import media

    found: list[str] = []
    seen: set[str] = set()
    for warc in sorted((ctx.output_dir / storage.WARC_DIR).glob("*.warc.gz")):
        try:
            with open(warc, "rb") as fh:
                for record in ArchiveIterator(fh):
                    if record.rec_type != "response" or not record.http_headers:
                        continue
                    if not _is_success(record.http_headers.get_statuscode()):
                        continue
                    ctype = record.http_headers.get_header("Content-Type", "") or ""
                    if "html" not in ctype.lower():
                        continue
                    base = record.rec_headers.get_header("WARC-Target-URI") or ""
                    body = record.content_stream().read(512 * 1024)
                    for url in media.find_embeds(body, base):
                        if url not in seen:
                            seen.add(url)
                            found.append(url)
        except Exception as exc:
            log.warning(
                "media scan could not read a WARC", extra={"warc": warc.name, "err": str(exc)}
            )
            continue
    return found


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
    blocked = 0
    redirects: list[tuple[str, str]] = []
    for warc in warcs:
        try:
            with open(warc, "rb") as fh:
                for record in ArchiveIterator(fh):
                    if record.rec_type != "response":
                        continue
                    if not record.http_headers:
                        continue
                    status = record.http_headers.get_statuscode()
                    # A redirect has no body to judge, and a redirect is how a
                    # gated blog most often refuses: the seed answers 302 and
                    # points somewhere the crawl may not follow. Checked before
                    # the success filter below, which would otherwise drop it.
                    if str(status or "").startswith("3"):
                        target = record.http_headers.get_header("Location", "") or ""
                        source = record.rec_headers.get_header("WARC-Target-URI") or ""
                        if target:
                            redirects.append((source, target))
                        continue
                    # Error pages are not pages. `--content-on-error` archives
                    # the body of every 404, and a site's 404 template
                    # references its own logo and stylesheet — which are then
                    # reported as assets the capture is missing, from a page
                    # nobody asked for. It also made the page count nonsense:
                    # 4 real pages and 12 mangled requests read as "16 pages".
                    if not _is_success(status):
                        continue
                    ctype = record.http_headers.get_header("Content-Type", "") or ""
                    if "html" not in ctype.lower():
                        continue
                    body = record.content_stream().read(512 * 1024)
                    scanned += 1
                    lazy_hits += sum(body.count(attr) for attr in LAZY_ATTRIBUTES)
                    base = record.rec_headers.get_header("WARC-Target-URI") or ""
                    referenced |= _referenced_assets(body, base)
                    # Counted in the pass that is already reading every page,
                    # rather than a second walk over gigabytes of WARC.
                    if interstitial.looks_blocked(body, base).blocked:
                        blocked += 1
        except Exception as exc:
            # An unreadable or truncated WARC downgrades the audit to a
            # warning; it must never turn a completed capture into a failure.
            log.warning(
                "asset audit could not read a WARC",
                extra={"warc": warc.name, "err": str(exc)},
            )
            continue

    _report_gate_redirects(ctx, redirects, scanned)

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
        ctx.warnings.append(_lazy_image_warning(ctx, lazy_hits, scanned))

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

    if blocked:
        share = blocked / scanned if scanned else 0
        ctx.warnings.append(
            f"{blocked} of {scanned} archived page(s) look like a content warning rather than "
            "content. The access profile's cookies were not accepted"
            + (
                " for any of this capture — check the profile covers this host, then run it again."
                if share > 0.8
                else " for part of this capture, most likely because they expired partway "
                "through. Re-mint or re-export the profile and capture again."
            )
        )
        if capture_is_ok(ctx):
            # Not "ok": the pages are there and they are the wrong pages. A
            # capture that reports success and contains four thousand copies
            # of an interstitial is the failure this whole feature exists to
            # prevent, and it must not be silent.
            ctx.capture.status = "partial"

    ctx.stats["interstitial_pages"] = blocked
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


def _engine_runs_javascript(ctx: Context) -> bool:
    """Whether the engine that made this capture executes page scripts.

    Read from the engine's own manifest rather than matched against a name.
    A warning that names one engine is wrong the moment a second one exists —
    which is exactly what happened: the lazy-image warning below told a
    browsertrix capture that wget cannot execute JavaScript, and advised
    recapturing with a browser engine to somebody who had just used one.

    Unknown engines are treated as not running scripts, because that is the
    warning that costs least when wrong: it says images may be missing when
    they are not, rather than saying the archive is fine when it is not.
    """
    # Guarded rather than looked up blind: a capture row that has not been
    # given its engine yet would otherwise reach `get()` with a null key, which
    # SQLAlchemy warns about and says it may one day refuse.
    engine_id = ctx.capture.engine_id
    if not engine_id:
        return False
    record = ctx.session.get(EngineRecord, engine_id)
    capabilities = (record.manifest or {}).get("capabilities") if record else None
    return bool((capabilities or {}).get("javascript"))


def _lazy_image_warning(ctx: Context, lazy_hits: int, scanned: int) -> str:
    """What lazy-loaded image references mean, which depends on the engine.

    On a non-scripting engine they are images the archive does not have. On a
    browser engine `autofetch` is meant to have pulled them, so the count is
    context rather than a problem — and `missing_assets` is what actually
    answers whether any of them are absent.
    """
    found = f"{lazy_hits} lazy-loaded image reference(s) found in {scanned} page(s). "
    if _engine_runs_javascript(ctx):
        return found + (
            "This engine runs page scripts and its autofetch behaviour is meant to "
            "pull them, so they are most likely archived — the missing-asset count "
            "above is what says otherwise."
        )
    return found + (
        f"{ctx.capture.engine_id} does not execute JavaScript, so those images are "
        "not in this archive. A browser engine would capture them."
    )


def _is_success(status: str | None) -> bool:
    return bool(status) and str(status).startswith("2")


def capture_is_ok(ctx: Context) -> bool:
    return ctx.capture.status == "ok"


def _report_gate_redirects(ctx: Context, redirects: list[tuple[str, str]], scanned: int) -> None:
    """Explain a capture that was turned away at the door.

    A gated blog does not serve a content warning to a crawler with no cookie
    — it **redirects** to one, on a host the crawl is not allowed to follow.
    So the archive holds a single 302, the capture reports one URL, and until
    this existed it said nothing at all about why. What the person then met
    was pywb, in an iframe, reporting that a URL they had never heard of could
    not be found in this collection.

    Two findings, in descending order of certainty:

    `gated` — the redirect target is a recognisable interstitial. That is the
    access-profile case and the message says so.

    `off-scope` — it is not recognisable, but the crawl could not follow it and
    archived nothing else. Whatever the reason, "the site sent us somewhere
    this capture is not allowed to go" is the explanation for an empty
    archive, and saying it is better than leaving somebody to work it out from
    a replay error.
    """
    if not redirects:
        return

    gated = [
        (source, target)
        for source, target in redirects
        if interstitial.url_looks_blocked(target).blocked
    ]
    ctx.stats["redirects"] = len(redirects)
    ctx.stats["gate_redirects"] = len(gated)

    if gated:
        source, target = gated[0]
        ctx.warnings.append(
            f"{len(gated)} request(s) were redirected to what looks like a content warning "
            f"rather than the page — {source} sends visitors to {target}. Nothing behind it "
            "was archived, because that host is not in this site's scope and would not be "
            "worth archiving if it were. Give this site an access profile that carries the "
            "cookie the warning sets, test it, and capture again."
        )
        if capture_is_ok(ctx):
            ctx.capture.status = "partial"
        return

    # No recognisable gate, and nothing else came back either.
    if scanned == 0:
        source, target = redirects[0]
        ctx.warnings.append(
            f"This capture archived no pages: {source} redirected to {target}, which is "
            "outside this site's scope, so the crawl stopped there. If that host is part of "
            "the site, turn it on in the domain picker; if it is a sign-in or a content "
            "warning, this site needs an access profile."
        )
        if capture_is_ok(ctx):
            ctx.capture.status = "partial"


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
    Step("cdxj-index", 40, False, step_cdxj_index),
    Step("text-extract", 50, False, step_text_extract),
    Step("asset-audit", 60, False, step_asset_audit),
    # Before media rather than beside it, which is where docs/05 first put it:
    # media downloads video and can run for an hour, and the card should not
    # wait behind that for a picture that takes a second.
    Step("screenshot", 65, False, step_thumbnail),
    # Last, and never required: it reaches hosts outside the site's scope and
    # can take longer than the crawl did.
    Step("media", 70, False, step_media),
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

    # Rewritten unconditionally, after everything has run. The manifest is
    # written at order 35 so that a chain that dies partway through still
    # leaves one, but every step after it — the index record count, the
    # extracted text, the asset audit, the media — accumulates stats the first
    # write cannot have seen. Rewriting only when there were warnings, which
    # is what this used to do, meant a capture that went perfectly recorded
    # less on disk than one that did not.
    capture.warc_files = ctx.artifacts
    if ctx.warnings:
        final = dict(ctx.stats)
        final["warnings"] = ctx.warnings
        ctx.stats = final
    step_manifest(ctx)

    session.flush()
    return ctx
