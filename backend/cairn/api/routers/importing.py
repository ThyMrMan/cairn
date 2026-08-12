"""Importing an ArchiveBox archive, and the metrics endpoint.

Both are edges of the application rather than parts of it: one reads somebody
else's archive, the other is read by somebody else's scraper.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status

from cairn.api.deps import AppSettings, ClientIp, Csrf, CurrentUser, DbSession
from cairn.api.errors import ApiError
from cairn.api.schemas import JobAccepted, MetricsSettings
from cairn.services import archivebox, audit, metrics

router = APIRouter(tags=["import"])

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _supervisor(request: Request) -> Any:
    return request.app.state.supervisor


def _root(path: str) -> Path:
    candidate = Path(path.strip())
    if not candidate.is_absolute():
        raise ApiError(
            "bad_path",
            "Give an absolute path to the ArchiveBox data directory as this container sees it.",
            status_code=400,
        )
    if not candidate.is_dir():
        raise ApiError(
            "not_found",
            f"{candidate} is not a directory this container can see. Mount your ArchiveBox "
            "data directory into the container and use the path inside it.",
            status_code=400,
        )
    return candidate


@router.get("/import/archivebox", dependencies=[Csrf])
def survey_archivebox(
    db: DbSession,
    _settings: AppSettings,
    _user: CurrentUser,
    path: Annotated[str, Query(min_length=1, max_length=1024)],
) -> dict[str, Any]:
    """What is in that archive, without touching it.

    The first question is always how much of it has a WARC: ArchiveBox
    archives plenty of pages with extractors that write none, and those cannot
    come across into a WARC archive.
    """
    try:
        return archivebox.survey(_root(path)).to_dict()
    except archivebox.ArchiveBoxError as exc:
        raise ApiError("unreadable", str(exc), status_code=400) from exc


@router.post(
    "/import/archivebox",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Csrf],
)
def import_archivebox(
    db: DbSession,
    user: CurrentUser,
    ip: ClientIp,
    supervisor: Annotated[Any, Depends(_supervisor)],
    path: Annotated[str, Query(min_length=1, max_length=1024)],
    host: Annotated[list[str] | None, Query()] = None,
    folder_id: Annotated[int | None, Query()] = None,
) -> JobAccepted:
    """Copy each domain's WARCs into a site of its own. The source is untouched."""
    root = _root(path)
    try:
        survey = archivebox.survey(root)
    except archivebox.ArchiveBoxError as exc:
        raise ApiError("unreadable", str(exc), status_code=400) from exc
    if not survey.with_warcs:
        raise ApiError(
            "nothing_to_import",
            "Nothing in that archive has a WARC, so there is nothing to import.",
            status_code=409,
        )

    job = supervisor.enqueue(
        db,
        job_type="import",
        site_id=None,
        spec={"path": str(root), "hosts": host or [], "folder_id": folder_id},
        priority=200,
    )
    audit.record(
        db,
        "import.archivebox",
        actor=user.username,
        target=str(root),
        ip=ip,
        detail={"job_id": job.id, "hosts": host or "all"},
    )
    db.commit()
    supervisor.notify()
    return JobAccepted(job_id=job.id)


# ── a pasted list of URLs ────────────────────────────────────────────────


@router.post("/import/urls/survey", dependencies=[Csrf])
def survey_urls(
    body: dict[str, str], db: DbSession, _settings: AppSettings, _user: CurrentUser
) -> dict[str, Any]:
    """What importing this list would do, before it does any of it."""
    from cairn.services import bulkurls

    try:
        return bulkurls.survey(db, body.get("text", "")).to_dict()
    except bulkurls.BulkImportError as exc:
        raise ApiError("bad_input", str(exc), status_code=400) from exc


@router.post("/import/urls", dependencies=[Csrf], status_code=status.HTTP_201_CREATED)
def import_urls(
    body: dict[str, Any],
    db: DbSession,
    settings: AppSettings,
    user: CurrentUser,
    ip: ClientIp,
    supervisor: Annotated[Any, Depends(_supervisor)],
) -> dict[str, Any]:
    """Create or reuse a site per domain and archive the pages that were listed.

    `crawl` is off unless asked for, and that is the whole safety property:
    a list of fifty bookmarks is fifty pages, not fifty crawls of fifty
    strangers' sites.
    """
    from cairn.services import bulkurls

    try:
        result = bulkurls.import_urls(
            db,
            settings,
            str(body.get("text") or ""),
            folder_id=body.get("folder_id"),
            tags=[str(t) for t in (body.get("tags") or [])],
            supervisor=supervisor,
            capture=bool(body.get("capture", True)),
            crawl=bool(body.get("crawl", False)),
        )
    except bulkurls.BulkImportError as exc:
        raise ApiError("bad_input", str(exc), status_code=400) from exc

    audit.record(
        db,
        "import.urls",
        actor=user.username,
        target=f"{len(result.created)} new site(s)",
        ip=ip,
        detail={"urls": result.urls, "jobs": len(result.jobs)},
    )
    db.commit()
    if result.jobs:
        supervisor.notify()
    return result.to_dict()


@router.get("/metrics/settings", dependencies=[Csrf])
def metrics_settings(db: DbSession, _user: CurrentUser) -> dict[str, Any]:
    """Whether the endpoint is on, and whether a token guards it.

    The token itself is never returned — only whether one is set. It is a
    credential, and this application does not hand credentials back out
    (docs/06, docs/11).
    """
    return {"enabled": metrics.enabled(db), "token_set": bool(metrics.token(db))}


@router.put("/metrics/settings", dependencies=[Csrf])
def set_metrics_settings(
    body: MetricsSettings, db: DbSession, user: CurrentUser, ip: ClientIp
) -> dict[str, Any]:
    from cairn.services import settings_store

    if body.enabled is not None:
        settings_store.put(db, metrics.ENABLED_SETTING, bool(body.enabled))
    if body.token is not None:
        settings_store.put(db, metrics.TOKEN_SETTING, body.token.strip())
    audit.record(
        db,
        "settings.metrics",
        actor=user.username,
        ip=ip,
        detail={"enabled": body.enabled, "token_set": bool((body.token or "").strip())},
    )
    return {"enabled": metrics.enabled(db), "token_set": bool(metrics.token(db))}


@router.get("/metrics")
def prometheus(request: Request, db: DbSession, settings: AppSettings) -> Response:
    """Prometheus exposition. Unauthenticated when enabled, and off by default.

    A scraper cannot log in, so this carries no site title, URL, host, folder
    or tag — only counts and durations with fixed-vocabulary labels. That is
    what makes leaving it open on a LAN a reasonable thing to do; `metrics.token`
    is there for anyone who disagrees.
    """
    if not metrics.enabled(db):
        raise ApiError(
            "disabled",
            "The metrics endpoint is off. Turn it on in Settings.",
            status_code=404,
        )

    expected = metrics.token(db)
    if expected:
        header = request.headers.get("Authorization", "")
        import secrets

        supplied = header[7:] if header.lower().startswith("bearer ") else ""
        if not secrets.compare_digest(supplied, expected):
            raise ApiError("unauthorized", "Bad or missing bearer token.", status_code=401)

    return Response(content=metrics.render(db, settings), media_type=PROMETHEUS_CONTENT_TYPE)
