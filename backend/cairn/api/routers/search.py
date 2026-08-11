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
