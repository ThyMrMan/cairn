"""Replay: what the UI needs to put an archived page on screen.

pywb serves the content on its own origin. Everything here is the *chrome*
around it — which captures exist, how many versions of a URL there are, and
what one raw record actually contains — and all of it is read from the index
and the WARCs directly rather than proxied from pywb. Two reasons:

  - The chrome keeps working when pywb is down, so one failure looks like one
    failure instead of an empty page with no explanation.
  - Proxying replayed bytes through the app origin would hand archived
    JavaScript the app's origin, which is the entire thing the separate port
    exists to prevent (docs/07, docs/11).

Raw payloads are therefore only ever served as attachments.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from cairn.api.deps import AppSettings, CurrentUser, DbSession
from cairn.api.errors import ApiError
from cairn.db.models import Site
from cairn.logging import get_logger
from cairn.services import replay
from cairn.services import sites as site_service

log = get_logger(__name__)

router = APIRouter(tags=["replay"])


def _require_site(db: DbSession, site_id: int) -> Site:
    site = site_service.get_site(db, site_id)
    if site is None:
        raise ApiError("not_found", "That site does not exist.", status_code=404)
    return site


# A raw payload is read into memory before it is sent. Archived responses are
# normally pages and images; anything past this is a video nobody wants to
# inspect record-by-record in a browser anyway.
MAX_INLINE_PAYLOAD = 32 * 1024 * 1024


@router.get("/sites/{site_id}/replay")
def replay_status(
    site_id: int, request: Request, db: DbSession, settings: AppSettings, _user: CurrentUser
) -> dict[str, Any]:
    """Whether this site can be replayed, and where."""
    site = _require_site(db, site_id)
    records, indexed_at = replay.index_stats(settings, site.archive_path)
    collection = replay.collection_name(site.id)
    # Same helper the CSP's frame-src uses. If these two ever disagree the
    # iframe is blocked by our own policy, so they share one implementation.
    origin = settings.replay_origin_for(request.url.scheme, request.url.hostname)

    # The app's own external port, which is the only evidence in here that the
    # deployment remaps ports at all. See `replay_port_is_assumed`.
    external_port = request.url.port or (443 if request.url.scheme == "https" else 80)

    return {
        "collection": collection,
        "records": records,
        # Records alone do not mean a browsable archive. A capture turned away
        # by a content warning holds one redirect; loading an iframe for that
        # shows pywb reporting a URL nobody asked for as missing.
        "pages": replay.replayable_pages(settings, site.archive_path),
        "indexed_at": indexed_at,
        "origin": origin,
        "base_url": f"{origin}/{collection}" if origin else "",
        "seed_url": site.seed_url,
        "shares_host_with_app": settings.replay_shares_host_with_app(),
        # Surfaced rather than silently hoped for: a wrong port here is a blank
        # iframe with the reason only in the browser console, which is the one
        # failure mode this whole tab cannot explain about itself.
        "port_is_assumed": settings.replay_port_is_assumed(external_port),
        "replay_port": settings.replay_public_port or settings.replay_port,
    }


@router.get("/sites/{site_id}/replay/versions")
def versions(
    site_id: int,
    db: DbSession,
    settings: AppSettings,
    _user: CurrentUser,
    url: str = Query(..., min_length=1, max_length=4096),
) -> dict[str, Any]:
    """Every capture of one URL, oldest first — the capture selector's data."""
    site = _require_site(db, site_id)
    try:
        found = replay.lookup(settings, site.archive_path, url)
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"index unreadable: {exc}") from exc
    return {"url": url, "count": len(found), "versions": [r.to_dict() for r in found]}


@router.post("/sites/{site_id}/reindex")
def reindex(
    site_id: int, db: DbSession, settings: AppSettings, _user: CurrentUser
) -> dict[str, Any]:
    """Rebuild the index from the WARCs.

    Cheap and safe at any time — the index is derived data. This is the first
    thing to try when replay 404s, which is why it is a button rather than a
    documented shell command.
    """
    site = _require_site(db, site_id)
    try:
        result = replay.build_index(settings, site.archive_path)
        replay.link_collection(settings, site.id, site.archive_path)
    except replay.ReplayError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"records": result.records, "warcs": result.warcs}


@router.get("/sites/{site_id}/replay/record")
def record(
    site_id: int,
    db: DbSession,
    settings: AppSettings,
    _user: CurrentUser,
    url: str = Query(..., min_length=1, max_length=4096),
    timestamp: str | None = Query(None, max_length=20),
    download: bool = False,
) -> Any:
    """One archived response, for inspection rather than rendering."""
    site = _require_site(db, site_id)
    found = replay.lookup(settings, site.archive_path, url)
    if timestamp:
        found = [r for r in found if r.timestamp == timestamp] or found
    if not found:
        raise HTTPException(status_code=404, detail="no archived record for that URL")

    chosen = found[-1]
    try:
        parsed = replay.read_record(settings, site.archive_path, chosen)
    except (replay.ReplayError, OSError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if download:
        payload = parsed.content_stream().read(MAX_INLINE_PAYLOAD)
        # Always an attachment, always octet-stream. Rendering archived bytes
        # inline on the app origin would reintroduce exactly the cross-origin
        # scripting that running pywb on its own port prevents.
        return StreamingResponse(
            iter([payload]),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="record-{chosen.timestamp}.bin"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    http_headers = parsed.http_headers
    return {
        **chosen.to_dict(),
        "record_type": parsed.rec_type,
        "http_status": http_headers.get_statuscode() if http_headers else None,
        "http_headers": dict(http_headers.headers) if http_headers else {},
        "warc_headers": dict(parsed.rec_headers.headers),
    }
