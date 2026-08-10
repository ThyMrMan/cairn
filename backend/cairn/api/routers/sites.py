"""Sites: create, list, edit, scope, and starting a capture (docs/09)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import func, select

from cairn.api.deps import AppSettings, ClientIp, Csrf, CurrentUser, DbSession
from cairn.api.errors import ApiError
from cairn.api.schemas import (
    CaptureRequest,
    CaptureSummary,
    HostRuleModel,
    JobAccepted,
    Ok,
    Page,
    ScopeModel,
    ScopeResponse,
    SiteCreate,
    SiteDetail,
    SiteSummary,
    SiteUpdate,
)
from cairn.db.models import Capture, Folder, Job, Site
from cairn.db.types import utcnow
from cairn.engines.registry import EngineConfigError, EngineError
from cairn.services import audit
from cairn.services import sites as site_service
from cairn.services.scope import HostRule, Scope, ScopeError, to_wget_args

router = APIRouter(tags=["sites"], dependencies=[Csrf])


# ── helpers ──────────────────────────────────────────────────────────────


def _registry(request: Request) -> Any:
    return request.app.state.registry


def _supervisor(request: Request) -> Any:
    return request.app.state.supervisor


def _require_site(db: DbSession, site_id: int) -> Site:
    site = site_service.get_site(db, site_id)
    if site is None:
        raise ApiError("not_found", "That site does not exist.", status_code=404)
    return site


def _scope_response(db: DbSession, site: Site) -> ScopeResponse:
    scope = site_service.resolved_scope(db, site)
    notes: list[str] = []
    preview: list[str] = []
    try:
        translated = to_wget_args(scope)
        notes = translated.notes
        preview = translated.args
    except ScopeError as exc:
        # An unusable scope must still be readable, or the user cannot open
        # the editor to fix the thing that made it unusable.
        notes = [f"This scope cannot run as-is: {exc}"]
    return ScopeResponse(
        seeds=scope.seeds,
        hosts=[HostRuleModel(**h.to_dict()) for h in scope.hosts],
        exclude_hosts=scope.exclude_hosts,
        accept_patterns=scope.accept_patterns,
        reject_patterns=scope.reject_patterns,
        path_prefix=scope.path_prefix,
        max_depth=scope.max_depth,
        max_pages=scope.max_pages,
        max_bytes=scope.max_bytes,
        obey_robots=scope.obey_robots,
        politeness=scope.politeness,
        notes=notes,
        wget_preview=preview,
    )


def _summary(db: DbSession, site: Site) -> SiteSummary:
    folder = db.get(Folder, site.folder_id)
    return SiteSummary(
        id=site.id,
        slug=site.slug,
        title=site.title,
        seed_url=site.seed_url,
        primary_host=site.primary_host,
        folder_id=site.folder_id,
        folder_path=folder.path if folder else "",
        status=site.status,
        engine_id=site.engine_id,
        profile_id=site.profile_id,
        keep_mirror=site.keep_mirror,
        tags=site_service.tags_for(db, site.id),
        size_bytes=site.size_bytes,
        url_count=site.url_count,
        archive_path=site.archive_path,
        last_capture_at=site.last_capture_at,
        created_at=site.created_at,
        updated_at=site.updated_at,
    )


# ── routes ───────────────────────────────────────────────────────────────


@router.get("/sites", response_model=Page[SiteSummary])
def list_sites(
    db: DbSession,
    _user: CurrentUser,
    folder_id: int | None = None,
    tag: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    q: str | None = None,
    sort: str = "-updated_at",
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
) -> Page[SiteSummary]:
    rows, total = site_service.list_sites(
        db,
        folder_id=folder_id,
        tag=tag,
        status=status_filter,
        q=q,
        sort=sort,
        page=page,
        per_page=per_page,
    )
    return Page[SiteSummary](
        items=[_summary(db, s) for s in rows], total=total, page=page, per_page=per_page
    )


@router.post("/sites", response_model=SiteDetail, status_code=status.HTTP_201_CREATED)
def create_site(
    body: SiteCreate,
    db: DbSession,
    settings: AppSettings,
    user: CurrentUser,
    ip: ClientIp,
    registry: Annotated[Any, Depends(_registry)],
) -> SiteDetail:
    try:
        registry.get(body.engine_id)
    except EngineError as exc:
        raise ApiError("unknown_engine", str(exc), status_code=400) from exc
    try:
        site = site_service.create_site(
            db,
            settings,
            seed_url=body.seed_url,
            title=body.title,
            folder_id=body.folder_id,
            notes=body.notes,
            engine_id=body.engine_id,
            profile_id=body.profile_id,
            keep_mirror=body.keep_mirror,
            tags=body.tags,
        )
    except site_service.SiteError as exc:
        raise ApiError("invalid_site", str(exc), status_code=400) from exc

    audit.record(db, "site.create", actor=user.username, target=site.slug, ip=ip)
    return _detail(db, site)


@router.get("/sites/{site_id}", response_model=SiteDetail)
def get_site(site_id: int, db: DbSession, _user: CurrentUser) -> SiteDetail:
    return _detail(db, _require_site(db, site_id))


def _detail(db: DbSession, site: Site) -> SiteDetail:
    base = _summary(db, site)
    captures = db.scalar(select(func.count(Capture.id)).where(Capture.site_id == site.id)) or 0
    running = db.scalar(
        select(Job.id)
        .where(Job.site_id == site.id, Job.status.in_(("queued", "running")))
        .order_by(Job.id.desc())
        .limit(1)
    )
    return SiteDetail(
        **base.model_dump(),
        notes=site.notes,
        engine_config=dict(site.engine_config or {}),
        scope=_scope_response(db, site),
        capture_count=captures,
        running_job_id=running,
    )


@router.patch("/sites/{site_id}", response_model=SiteDetail)
def update_site(
    site_id: int,
    body: SiteUpdate,
    db: DbSession,
    settings: AppSettings,
    user: CurrentUser,
    ip: ClientIp,
    registry: Annotated[Any, Depends(_registry)],
) -> SiteDetail:
    site = _require_site(db, site_id)

    if body.engine_id is not None:
        try:
            registry.get(body.engine_id)
        except EngineError as exc:
            raise ApiError("unknown_engine", str(exc), status_code=400) from exc
        site.engine_id = body.engine_id

    if body.engine_config is not None:
        engine = registry.get(body.engine_id or site.engine_id)
        try:
            site.engine_config = engine.validate_config(body.engine_config)
        except EngineConfigError as exc:
            raise ApiError(
                "invalid_config", str(exc), status_code=422, detail=exc.problems
            ) from exc

    for field in ("title", "notes", "keep_mirror"):
        value = getattr(body, field)
        if value is not None:
            setattr(site, field, value)
    if body.profile_id is not None:
        site.profile_id = body.profile_id or None
    if body.folder_id is not None and body.folder_id != site.folder_id:
        raise ApiError(
            "not_implemented",
            "Moving a site between folders arrives with the folder tree in M4.",
            status_code=501,
        )
    if body.tags is not None:
        site_service.set_tags(db, site, body.tags)

    site.updated_at = utcnow()
    db.flush()
    site_service.write_site_yaml(db, settings, site)
    audit.record(db, "site.update", actor=user.username, target=site.slug, ip=ip)
    return _detail(db, site)


@router.delete("/sites/{site_id}", response_model=Ok)
def delete_site(
    site_id: int, db: DbSession, settings: AppSettings, user: CurrentUser, ip: ClientIp
) -> Ok:
    site = _require_site(db, site_id)
    running = db.scalar(
        select(func.count(Job.id)).where(
            Job.site_id == site.id, Job.status.in_(("queued", "running"))
        )
    )
    if running:
        raise ApiError(
            "site_busy",
            "A capture is still running for this site. Cancel it first.",
            status_code=409,
        )
    site_service.soft_delete(db, settings, site)
    audit.record(db, "site.delete", actor=user.username, target=site.slug, ip=ip)
    return Ok()


# ── scope ────────────────────────────────────────────────────────────────


@router.get("/sites/{site_id}/scope", response_model=ScopeResponse)
def get_scope(site_id: int, db: DbSession, _user: CurrentUser) -> ScopeResponse:
    return _scope_response(db, _require_site(db, site_id))


@router.put("/sites/{site_id}/scope", response_model=ScopeResponse)
def put_scope(
    site_id: int,
    body: ScopeModel,
    db: DbSession,
    settings: AppSettings,
    user: CurrentUser,
    ip: ClientIp,
) -> ScopeResponse:
    site = _require_site(db, site_id)
    scope = Scope(
        seeds=[site.seed_url],
        hosts=[
            HostRule(
                host=h.host.strip().lower(),
                crawl_pages=h.crawl_pages,
                fetch_assets=h.fetch_assets,
                path_prefix=h.path_prefix,
                allow_extensionless=h.allow_extensionless,
            )
            for h in body.hosts
        ],
        exclude_hosts=[h.strip().lower() for h in body.exclude_hosts],
        accept_patterns=body.accept_patterns,
        reject_patterns=body.reject_patterns,
        path_prefix=body.path_prefix,
        max_depth=body.max_depth,
        max_pages=body.max_pages,
        max_bytes=body.max_bytes,
        obey_robots=body.obey_robots,
    )
    scope.politeness.update(body.politeness or {})

    try:
        site_service.save_scope(db, site, scope)
    except ScopeError as exc:
        raise ApiError("scope_invalid", str(exc), status_code=422) from exc

    site_service.write_site_yaml(db, settings, site)
    audit.record(db, "site.scope", actor=user.username, target=site.slug, ip=ip)
    return _scope_response(db, site)


# ── captures ─────────────────────────────────────────────────────────────


@router.post(
    "/sites/{site_id}/capture", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED
)
def start_capture(
    site_id: int,
    body: CaptureRequest,
    db: DbSession,
    user: CurrentUser,
    ip: ClientIp,
    registry: Annotated[Any, Depends(_registry)],
    supervisor: Annotated[Any, Depends(_supervisor)],
) -> JobAccepted:
    site = _require_site(db, site_id)

    existing = db.scalar(
        select(Job.id).where(Job.site_id == site.id, Job.status.in_(("queued", "running"))).limit(1)
    )
    if existing:
        raise ApiError(
            "already_running",
            "A capture for this site is already queued or running.",
            status_code=409,
            detail={"job_id": existing},
        )

    # Validate before queueing: a job that fails the instant it is claimed is
    # a worse experience than a request that says why up front.
    try:
        engine = registry.get(site.engine_id)
        engine.validate_config(dict(site.engine_config or {}))
    except EngineConfigError as exc:
        raise ApiError("invalid_config", str(exc), status_code=422, detail=exc.problems) from exc
    except EngineError as exc:
        raise ApiError("unknown_engine", str(exc), status_code=400) from exc

    try:
        to_wget_args(site_service.resolved_scope(db, site))
    except ScopeError as exc:
        raise ApiError("scope_invalid", str(exc), status_code=422) from exc

    job = supervisor.enqueue(
        db,
        job_type="capture",
        site_id=site.id,
        spec={"kind": body.kind, "extra_seeds": body.extra_seeds},
    )
    audit.record(
        db,
        "capture.start",
        actor=user.username,
        target=site.slug,
        ip=ip,
        detail={"job_id": job.id, "kind": body.kind},
    )
    db.commit()
    supervisor.notify()
    return JobAccepted(job_id=job.id)


@router.get("/sites/{site_id}/captures", response_model=list[CaptureSummary])
def list_captures(site_id: int, db: DbSession, _user: CurrentUser) -> list[CaptureSummary]:
    site = _require_site(db, site_id)
    rows = db.scalars(
        select(Capture).where(Capture.site_id == site.id).order_by(Capture.started_at.desc())
    ).all()
    return [
        CaptureSummary(
            id=c.id,
            site_id=c.site_id,
            job_id=c.job_id,
            kind=c.kind,
            engine_id=c.engine_id,
            engine_version=c.engine_version,
            dir_name=c.dir_name,
            status=c.status,
            started_at=c.started_at,
            finished_at=c.finished_at,
            url_count=c.url_count,
            error_count=c.error_count,
            bytes_written=c.bytes_written,
        )
        for c in rows
    ]
