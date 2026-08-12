"""Full-text search over every archive (docs/09).

Read-only and cheap: one FTS5 lookup, one join, and a seek per result into the
extracted text for the snippet. The expensive half — extracting text and
maintaining the index — happens after a capture, and its rebuild is a job.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status

from cairn.api.deps import AppSettings, ClientIp, Csrf, CurrentUser, DbSession
from cairn.api.errors import ApiError
from cairn.api.schemas import JobAccepted, SearchHit, SearchResults, SearchStatus
from cairn.services import audit, search

router = APIRouter(tags=["search"], dependencies=[Csrf])


def _supervisor(request: Request) -> Any:
    return request.app.state.supervisor


@router.get("/search", response_model=SearchResults)
def run_search(
    db: DbSession,
    settings: AppSettings,
    _user: CurrentUser,
    q: Annotated[str, Query(max_length=512)] = "",
    site_id: Annotated[int | None, Query()] = None,
    folder: Annotated[str | None, Query(max_length=1024)] = None,
    tag: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=search.MAX_LIMIT)] = search.DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SearchResults:
    try:
        results = search.search(
            db,
            settings,
            query=q,
            site_id=site_id,
            folder=folder,
            tag=tag,
            limit=limit,
            offset=offset,
        )
    except search.SearchError as exc:
        # Reaching this means parse_query let something through, which is a
        # bug rather than bad input — but a 500 on a search box is worse than
        # a message saying the query could not be run.
        raise ApiError(
            "bad_query", f"That search could not be run: {exc}", status_code=400
        ) from exc

    return SearchResults(
        query=results.query,
        terms=results.terms,
        total=results.total,
        truncated=results.truncated,
        hits=[
            SearchHit(
                site_id=hit.site_id,
                site_title=hit.site_title,
                site_slug=hit.site_slug,
                folder_path=hit.folder_path,
                url=hit.url,
                title=hit.title,
                snippets=hit.snippets,
                score=hit.score,
                capture_id=hit.capture_id,
                timestamp=hit.timestamp,
                words=hit.words,
            )
            for hit in results.hits
        ],
    )


@router.get("/search/status", response_model=SearchStatus)
def search_status(db: DbSession, _settings: AppSettings, _user: CurrentUser) -> SearchStatus:
    stats = search.stats(db)
    return SearchStatus(
        pages=stats["pages"],
        words=stats["words"],
        sites=stats["sites"],
        unindexed_sites=search.unindexed_sites(db),
    )


@router.post(
    "/maintenance/reindex-search",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def reindex_search(
    db: DbSession,
    user: CurrentUser,
    ip: ClientIp,
    supervisor: Annotated[Any, Depends(_supervisor)],
    site_id: Annotated[int | None, Query()] = None,
    extract: Annotated[bool, Query()] = False,
) -> JobAccepted:
    """Rebuild the index from the extracted text, or from the WARCs.

    Without `extract` this reads only `derived/text/`, which is the fast path
    and the right one after a database restore. With it, every WARC is read
    again — needed when the extractor itself has changed, or when a capture
    predates text extraction entirely.
    """
    job = supervisor.enqueue(
        db, job_type="index", site_id=site_id, spec={"extract": extract}, priority=200
    )
    audit.record(
        db,
        "search.reindex",
        actor=user.username,
        target=str(site_id) if site_id else "all sites",
        ip=ip,
        detail={"job_id": job.id, "extract": extract},
    )
    db.commit()
    supervisor.notify()
    return JobAccepted(job_id=job.id)


# ── the reader view ──────────────────────────────────────────────────────
#
# Beside replay rather than instead of it. Replay answers "is this what was
# published"; this answers "I want to read this", which is a different question
# and the one asked more often once an archive is a few years old.


@router.get("/sites/{site_id}/reader")
def read_page(
    site_id: int,
    db: DbSession,
    settings: AppSettings,
    _user: CurrentUser,
    url: Annotated[str, Query(min_length=1, max_length=4096)],
    capture: Annotated[str | None, Query(max_length=255)] = None,
) -> dict[str, Any]:
    """One archived page as clean text."""
    from cairn.services import reader

    site = _require_site(db, site_id)
    article = reader.read(db, settings, site, url, capture_dir=capture)
    if article is None:
        raise ApiError(
            "no_text",
            "There is no extracted text for that page. Captures made before text "
            "extraction existed need Rebuild search index with re-extraction before "
            "they can be read.",
            status_code=404,
        )
    from cairn.services import annotations as annotation_service

    # Placed against *this* article rather than stored as positions, so a note
    # made on one capture shows on another when the sentence survived — and
    # says it is lost when it did not.
    return {
        **article.to_dict(),
        "annotations": annotation_service.resolve(db, site.id, article),
    }


@router.get("/sites/{site_id}/reader/versions")
def read_versions(
    site_id: int,
    db: DbSession,
    settings: AppSettings,
    _user: CurrentUser,
    url: Annotated[str, Query(min_length=1, max_length=4096)],
) -> dict[str, Any]:
    """Which captures can be read, oldest first."""
    from cairn.services import reader

    site = _require_site(db, site_id)
    found = reader.versions(db, settings, site, url)
    return {"url": url, "versions": [v.to_dict() for v in found]}


@router.get("/sites/{site_id}/reader/index")
def read_index(
    site_id: int,
    db: DbSession,
    _user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Every readable page of a site — the way in without a URL."""
    from cairn.services import reader

    site = _require_site(db, site_id)
    return reader.index_of(db, site, limit=limit, offset=offset)


def _require_site(db: DbSession, site_id: int) -> Any:
    from cairn.services import sites as site_service

    site = site_service.get_site(db, site_id)
    if site is None:
        raise ApiError("not_found", "That site does not exist.", status_code=404)
    return site


# ── annotations ──────────────────────────────────────────────────────────
#
# On the reader rather than on replay, and not by preference: replay is a
# separate origin so archived JavaScript cannot reach this application, which
# means this application cannot read a selection out of it either. See
# `services/annotations.py`.


@router.get("/sites/{site_id}/annotations")
def list_annotations(
    site_id: int,
    db: DbSession,
    _user: CurrentUser,
    url: Annotated[str | None, Query(max_length=4096)] = None,
) -> dict[str, Any]:
    from cairn.services import annotations as annotation_service

    site = _require_site(db, site_id)
    rows = (
        annotation_service.for_page(db, site.id, url)
        if url
        else annotation_service.for_site(db, site.id)
    )
    return {"annotations": [annotation_service.to_dict(row) for row in rows]}


@router.post("/sites/{site_id}/annotations", status_code=status.HTTP_201_CREATED)
def create_annotation(
    site_id: int, body: dict[str, Any], db: DbSession, user: CurrentUser, ip: ClientIp
) -> dict[str, Any]:
    from cairn.services import annotations as annotation_service

    site = _require_site(db, site_id)
    try:
        row = annotation_service.create(
            db,
            site,
            url=str(body.get("url") or ""),
            quote=str(body.get("quote") or ""),
            note=body.get("note"),
            prefix=str(body.get("prefix") or ""),
            suffix=str(body.get("suffix") or ""),
            block_index=int(body.get("block_index") or 0),
            color=str(body.get("color") or "yellow"),
        )
    except annotation_service.AnnotationError as exc:
        raise ApiError("invalid_annotation", str(exc), status_code=422) from exc
    audit.record(db, "annotation.create", actor=user.username, target=site.slug, ip=ip)
    return annotation_service.to_dict(row)


@router.patch("/annotations/{annotation_id}")
def edit_annotation(
    annotation_id: int, body: dict[str, Any], db: DbSession, _user: CurrentUser
) -> dict[str, Any]:
    from cairn.services import annotations as annotation_service

    row = annotation_service.get(db, annotation_id)
    if row is None:
        raise ApiError("not_found", "That annotation does not exist.", status_code=404)
    try:
        annotation_service.update(db, row, note=body.get("note"), color=body.get("color"))
    except annotation_service.AnnotationError as exc:
        raise ApiError("invalid_annotation", str(exc), status_code=422) from exc
    return annotation_service.to_dict(row)


@router.delete("/annotations/{annotation_id}")
def delete_annotation(annotation_id: int, db: DbSession, _user: CurrentUser) -> dict[str, bool]:
    from cairn.services import annotations as annotation_service

    row = annotation_service.get(db, annotation_id)
    if row is None:
        raise ApiError("not_found", "That annotation does not exist.", status_code=404)
    annotation_service.remove(db, row)
    return {"ok": True}
