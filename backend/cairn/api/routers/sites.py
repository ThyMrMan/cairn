"""Sites: create, list, edit, scope, and starting a capture (docs/09)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select

from cairn.api.deps import AppSettings, ClientIp, Csrf, CurrentUser, DbSession
from cairn.api.errors import ApiError
from cairn.api.schemas import (
    BulkSiteRequest,
    BulkSiteResponse,
    CaptureRequest,
    CaptureSummary,
    CompanionPassRequest,
    HostRuleModel,
    JobAccepted,
    MediaPolicy,
    MoveOutcome,
    Ok,
    Page,
    ScopeModel,
    ScopeResponse,
    SiteCreate,
    SiteDetail,
    SiteMoveRequest,
    SiteSummary,
    SiteUpdate,
)
from cairn.config import Settings
from cairn.db.models import Capture, Folder, Job, Site
from cairn.db.types import utcnow
from cairn.engines.registry import EngineConfigError, EngineError
from cairn.services import audit, discovery_service, media, moves, symlinks, thumbnail, trash
from cairn.services import folders as folder_service
from cairn.services import profiles as profile_service
from cairn.services import sites as site_service
from cairn.services import tags as tag_service
from cairn.services.filters import FilterError, SiteFilter
from cairn.services.scope import HostRule, Scope, ScopeError, to_wget_args
from cairn.services.storage import CrossDeviceMoveError, StoragePathError

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


def _summary(
    db: DbSession, settings: Settings, site: Site, tags: list[str] | None = None
) -> SiteSummary:
    folder = db.get(Folder, site.folder_id)
    return SiteSummary(
        has_thumbnail=thumbnail.exists(settings, site.archive_path),
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
        tags=site_service.tags_for(db, site.id) if tags is None else tags,
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
    request: Request,
    db: DbSession,
    settings: AppSettings,
    _user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
) -> Page[SiteSummary]:
    """Filter, sort and page.

    The filter is read straight off the query string rather than declared as
    parameters, because `tag` repeats and because every one of these fields
    also has to survive a round trip through a saved view. One reader for both
    is the only way those two stay the same thing (docs/09).
    """
    try:
        site_filter = _filter_from(request)
    except FilterError as exc:
        raise ApiError("invalid_filter", str(exc), status_code=400) from exc

    rows, total = site_service.list_sites(db, site_filter, page=page, per_page=per_page)
    tags = site_service.tag_map(db, [s.id for s in rows])
    return Page[SiteSummary](
        items=[_summary(db, settings, s, tags.get(s.id, [])) for s in rows],
        total=total,
        page=page,
        per_page=per_page,
    )


def _filter_from(request: Request) -> SiteFilter:
    raw: dict[str, Any] = dict(request.query_params)
    repeated = request.query_params.getlist("tag")
    if repeated:
        raw["tag"] = repeated
    return SiteFilter.from_params(raw)


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
    return _detail(db, settings, site)


@router.get("/sites/{site_id}", response_model=SiteDetail)
def get_site(site_id: int, db: DbSession, settings: AppSettings, _user: CurrentUser) -> SiteDetail:
    return _detail(db, settings, _require_site(db, site_id))


def _detail(db: DbSession, settings: Settings, site: Site) -> SiteDetail:
    base = _summary(db, settings, site)
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
        # Only the detail view carries this, not the summary: the engine picker
        # is the one thing that needs it, and putting it on the summary would
        # be a profile lookup per row on every list page.
        profile_has_browser_profile=(
            site.profile is not None and profile_service.has_browser_profile(site.profile)
        ),
        profile_has_cookies=(site.profile is not None and site.profile.cookies_enc is not None),
        companion_pass=(
            pass_.to_dict() if (pass_ := discovery_service.companion_pass_for(site)) else None
        ),
        preset=discovery_service.applied_preset(site),
    )


@router.get("/sites/{site_id}/thumbnail")
def site_thumbnail(
    site_id: int, request: Request, db: DbSession, settings: AppSettings, _user: CurrentUser
) -> Response:
    """The site card's picture of the archived front page.

    Our own render, not an archived byte — so unlike the raw record inspector
    (docs/11) it is served inline, which is the entire point of it. `nosniff`
    and a fixed content type all the same: the file is written by this
    application and by nothing else, and a served path should never be the
    place that assumption is first tested.
    """
    site = _require_site(db, site_id)
    path = thumbnail.image_path(settings, site.archive_path)
    if not path.is_file():
        raise ApiError("not_found", "This site has no thumbnail.", status_code=404)

    stat = path.stat()
    etag = f'"{int(stat.st_mtime)}-{stat.st_size}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
    return Response(
        content=path.read_bytes(),
        media_type=thumbnail.CONTENT_TYPE,
        headers={
            "ETag": etag,
            # Long enough that a list of two hundred sites is not two hundred
            # conditional requests every time somebody sorts the page; short
            # enough that a capture taken while you are looking shows up.
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
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
    supervisor: Annotated[Any, Depends(_supervisor)],
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
    if body.tags is not None:
        try:
            site_service.set_tags(db, site, body.tags)
        except site_service.SiteError as exc:
            raise ApiError("invalid_tag", str(exc), status_code=400) from exc
        symlinks.safe_rebuild(db, settings)

    if body.folder_id is not None and body.folder_id != site.folder_id:
        # A folder change is a directory move, so it goes through the same
        # path as an explicit one — including the chance of being a copy.
        outcome = _move(db, settings, site, body.folder_id, supervisor)
        if outcome.status == "queued":
            raise ApiError(
                "cross_device",
                "That folder is on a different filesystem, so the archive has to be "
                "copied rather than moved. Use Move to run it as a job you can watch.",
                status_code=409,
                detail={"job_id": outcome.job_id},
            )

    site.updated_at = utcnow()
    db.flush()
    site_service.write_site_yaml(db, settings, site)
    audit.record(db, "site.update", actor=user.username, target=site.slug, ip=ip)
    return _detail(db, settings, site)


@router.post("/sites/{site_id}/move", response_model=MoveOutcome)
def move_site(
    site_id: int,
    body: SiteMoveRequest,
    db: DbSession,
    settings: AppSettings,
    user: CurrentUser,
    ip: ClientIp,
    supervisor: Annotated[Any, Depends(_supervisor)],
) -> MoveOutcome:
    site = _require_site(db, site_id)
    outcome = _move(db, settings, site, body.folder_id, supervisor)
    audit.record(
        db,
        "site.move",
        actor=user.username,
        target=site.slug,
        ip=ip,
        detail={"to": outcome.path, "status": outcome.status},
    )
    return outcome


def _move(db: DbSession, settings: Any, site: Site, folder_id: int, supervisor: Any) -> MoveOutcome:
    try:
        target = folder_service.require_folder(db, folder_id)
    except folder_service.FolderError as exc:
        raise ApiError("not_found", str(exc), status_code=404) from exc

    try:
        result = moves.move_site(db, settings, site, target)
    except CrossDeviceMoveError:
        spec = {"op": "move-site", "site_id": site.id, "target_folder_id": folder_id}
        job = supervisor.enqueue(db, job_type="move", site_id=site.id, spec=spec)
        db.commit()
        supervisor.notify()
        return MoveOutcome(status="queued", method="copy", path=site.archive_path, job_id=job.id)
    except moves.SiteBusyError as exc:
        raise ApiError("site_busy", str(exc), status_code=409) from exc
    except (moves.MoveError, OSError) as exc:
        raise ApiError("move_failed", str(exc), status_code=409) from exc
    return MoveOutcome(status="done", method=result.method, path=result.new_path)


@router.delete("/sites/{site_id}", response_model=Ok)
def delete_site(
    site_id: int,
    db: DbSession,
    settings: AppSettings,
    user: CurrentUser,
    ip: ClientIp,
    purge: bool = Query(default=False),
) -> Ok:
    """Move to the trash, or — with `?purge=true` — delete for good."""
    site = _require_site(db, site_id)
    slug = site.slug
    try:
        trash.trash_site(db, settings, site)
        if purge:
            trash.purge_site(db, settings, site)
    except trash.TrashError as exc:
        raise ApiError("site_busy", str(exc), status_code=409) from exc

    audit.record(
        db,
        "site.purge" if purge else "site.delete",
        actor=user.username,
        target=slug,
        ip=ip,
    )
    return Ok()


@router.post("/sites/{site_id}/restore", response_model=SiteDetail)
def restore_site(
    site_id: int, db: DbSession, settings: AppSettings, user: CurrentUser, ip: ClientIp
) -> SiteDetail:
    site = db.get(Site, site_id)
    if site is None:
        raise ApiError("not_found", "That site does not exist.", status_code=404)
    try:
        trash.restore_site(db, settings, site)
    except trash.TrashError as exc:
        raise ApiError("restore_failed", str(exc), status_code=409) from exc
    except OSError as exc:
        raise ApiError(
            "restore_failed", f"the archive could not be moved back: {exc}", status_code=500
        ) from exc

    audit.record(db, "site.restore", actor=user.username, target=site.slug, ip=ip)
    return _detail(db, settings, site)


@router.post("/sites/bulk", response_model=BulkSiteResponse)
def bulk_update(
    body: BulkSiteRequest,
    db: DbSession,
    settings: AppSettings,
    user: CurrentUser,
    ip: ClientIp,
    supervisor: Annotated[Any, Depends(_supervisor)],
) -> BulkSiteResponse:
    """Tag, untag and move many sites in one request.

    Partial success is the honest outcome here. One site in a selection of
    twenty being mid-capture should not stop the other nineteen from being
    tagged, so what could not be done comes back named rather than as a failed
    request that leaves the user guessing which one it was.
    """
    live = list(
        db.scalars(select(Site).where(Site.id.in_(body.site_ids), Site.deleted_at.is_(None))).all()
    )
    ids = [s.id for s in live]

    try:
        tagged = tag_service.add_to_sites(db, ids, body.add_tags)
        untagged = tag_service.remove_from_sites(db, ids, body.remove_tags)
    except tag_service.TagError as exc:
        raise ApiError("invalid_tag", str(exc), status_code=400) from exc

    moved = 0
    queued: list[int] = []
    skipped: list[str] = []
    if body.folder_id is not None:
        try:
            target = folder_service.require_folder(db, body.folder_id)
        except folder_service.FolderError as exc:
            raise ApiError("not_found", str(exc), status_code=404) from exc
        for site in live:
            try:
                result = moves.move_site(db, settings, site, target)
                moved += 1 if result.method != "noop" else 0
            except CrossDeviceMoveError:
                job = supervisor.enqueue(
                    db,
                    job_type="move",
                    site_id=site.id,
                    spec={"op": "move-site", "site_id": site.id, "target_folder_id": target.id},
                )
                queued.append(job.id)
            except (moves.MoveError, OSError) as exc:
                skipped.append(f"{site.title}: {exc}")

    if body.add_tags or body.remove_tags:
        symlinks.safe_rebuild(db, settings)
    audit.record(
        db,
        "site.bulk",
        actor=user.username,
        target=f"{len(ids)} site(s)",
        ip=ip,
        detail={"tagged": tagged, "untagged": untagged, "moved": moved},
    )
    if queued:
        db.commit()
        supervisor.notify()
    return BulkSiteResponse(
        tagged=tagged, untagged=untagged, moved=moved, queued_job_ids=queued, skipped=skipped
    )


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
        seeds=site_service.all_seeds(site),
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

    # Remember that a person chose this, so re-running discovery reports what
    # changed instead of overwriting the selection.
    site.scope_settings = {**(site.scope_settings or {}), "user_edited": True}
    site_service.write_site_yaml(db, settings, site)
    audit.record(db, "site.scope", actor=user.username, target=site.slug, ip=ip)
    return _scope_response(db, site)


# ── seeds ────────────────────────────────────────────────────────────────
#
# A site can start from more than one place: a blog that moved to a custom
# domain, or one that spans two. All the seeds share one scope, one index and
# one replay collection, because they are one site — splitting them into two
# gives you two half-histories and a capture selector that lies about which
# versions of a page exist (docs/13).


@router.get("/sites/{site_id}/seeds")
def list_seeds(site_id: int, db: DbSession, _user: CurrentUser) -> dict[str, Any]:
    site = _require_site(db, site_id)
    return {
        "primary": site.seed_url,
        "seeds": site_service.all_seeds(site),
        "max": site_service.MAX_SEEDS_PER_SITE,
    }


@router.post("/sites/{site_id}/seeds", status_code=status.HTTP_201_CREATED)
def add_seed(
    site_id: int,
    body: dict[str, str],
    db: DbSession,
    settings: AppSettings,
    user: CurrentUser,
    ip: ClientIp,
) -> dict[str, Any]:
    site = _require_site(db, site_id)
    try:
        added = site_service.add_seed(db, settings, site, body.get("url", ""))
    except site_service.SiteError as exc:
        raise ApiError("seed_invalid", str(exc), status_code=422) from exc

    audit.record(db, "site.seed.add", actor=user.username, target=site.slug, ip=ip)
    return {
        "primary": site.seed_url,
        "seeds": site_service.all_seeds(site),
        "added": added,
        # Said rather than done: re-indexing is a job, and doing it silently
        # because somebody typed a URL is the sort of surprise that makes
        # people afraid of a button.
        "note": (
            "Re-index the site so the picker sees this domain's asset hosts before "
            "the next capture."
        ),
    }


@router.delete("/sites/{site_id}/seeds", response_model=Ok)
def delete_seed(
    site_id: int,
    db: DbSession,
    settings: AppSettings,
    user: CurrentUser,
    ip: ClientIp,
    url: str = Query(..., min_length=1, max_length=4096),
) -> Ok:
    site = _require_site(db, site_id)
    try:
        site_service.remove_seed(db, settings, site, url)
    except site_service.SiteError as exc:
        raise ApiError("seed_invalid", str(exc), status_code=422) from exc
    audit.record(db, "site.seed.remove", actor=user.username, target=site.slug, ip=ip)
    return Ok()


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


@router.post(
    "/sites/{site_id}/capture/companion",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_companion_pass(
    site_id: int,
    body: CompanionPassRequest,
    db: DbSession,
    user: CurrentUser,
    ip: ClientIp,
    supervisor: Annotated[Any, Depends(_supervisor)],
) -> JobAccepted:
    """Run the second, cheap capture this site's preset offers.

    Separate from `/capture` rather than another `kind` on it, because it is a
    different question: `/capture` archives the site, and this fills a gap the
    site's own scope was configured to leave. Conflating them would put "which
    preset is applied" into the meaning of a general endpoint.
    """
    site = _require_site(db, site_id)
    companion = discovery_service.companion_pass_for(site)
    if companion is None:
        raise ApiError(
            "no_companion_pass",
            "This site's preset does not offer a second pass. It is available on presets "
            "that deliberately skip something a cheaper crawl can fetch afterwards — the "
            "lean Blogger preset and its pagination trail.",
            status_code=400,
        )
    if body.pass_id and body.pass_id != companion.id:
        raise ApiError(
            "unknown_companion_pass",
            f"This site offers the {companion.id!r} pass, not {body.pass_id!r}.",
            status_code=400,
        )

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

    job = supervisor.enqueue(
        db,
        job_type="capture",
        site_id=site.id,
        spec={"kind": "companion", "pass": companion.id},
    )
    audit.record(
        db,
        "capture.companion",
        actor=user.username,
        target=site.slug,
        ip=ip,
        detail={"job_id": job.id, "pass": companion.id},
    )
    db.commit()
    supervisor.notify()
    return JobAccepted(job_id=job.id)


# ── embedded media ───────────────────────────────────────────────────────
#
# The post-processor that downloads embedded video has been in the chain since
# M9, and until now nothing could switch it on: the policy lives in
# `scope_settings["media"]` and no endpoint wrote it, so the only way to enable
# the feature was to edit the database by hand. These three endpoints are what
# make it a feature rather than an implementation.


def _media_captures(db: DbSession, site: Site, limit: int = 50) -> list[Capture]:
    return list(
        db.scalars(
            select(Capture)
            .where(Capture.site_id == site.id)
            .order_by(Capture.started_at.desc())
            .limit(limit)
        ).all()
    )


def _media_payload(db: DbSession, settings: Settings, site: Site) -> dict[str, Any]:
    """One shape for both reading and writing, so they cannot drift.

    `policy` is already merged — built-in under instance setting under site
    override — because the layering is invisible in the UI and a form that
    edits one layer while displaying another is a form that lies. `instance`
    and `override` come along so the UI can say which of the two a value came
    from, and offer a way back to the default.
    """
    from cairn.services import settings_store

    ok, reason = media.available()
    return {
        "policy": media.policy_for(db, site),
        "instance": settings_store.get(db, media.SETTING, {}) or {},
        "override": (site.scope_settings or {}).get("media") or {},
        "available": ok,
        "unavailable_reason": reason,
        "hosts": list(media.EMBED_HOSTS),
        **media.library(settings, site, _media_captures(db, site)),
    }


@router.get("/sites/{site_id}/media")
def get_media(
    site_id: int, db: DbSession, settings: AppSettings, _user: CurrentUser
) -> dict[str, Any]:
    """The effective policy, whether it can run, and what it has collected."""
    return _media_payload(db, settings, _require_site(db, site_id))


@router.put("/sites/{site_id}/media")
def set_media(
    site_id: int,
    body: MediaPolicy,
    db: DbSession,
    settings: AppSettings,
    user: CurrentUser,
    ip: ClientIp,
) -> dict[str, Any]:
    """Set this site's media policy. Only the fields sent are overridden."""
    site = _require_site(db, site_id)
    override = body.model_dump(exclude_none=True)
    scope_settings = dict(site.scope_settings or {})
    if override:
        scope_settings["media"] = override
    else:
        # An empty body clears the override and returns the site to whatever
        # the instance default is, which is the only way back to "inherit".
        scope_settings.pop("media", None)
    site.scope_settings = scope_settings
    db.flush()
    audit.record(
        db,
        "media.policy",
        actor=user.username,
        target=site.slug,
        ip=ip,
        detail=override,
    )
    return _media_payload(db, settings, site)


@router.get("/sites/{site_id}/media/{capture_dir}/{filename}")
def media_file(
    site_id: int,
    capture_dir: str,
    filename: str,
    db: DbSession,
    settings: AppSettings,
    _user: CurrentUser,
) -> FileResponse:
    """Serve one downloaded file, so an archived video can be watched.

    Unlike a WACZ export — a zip of untrusted archived bytes, always an
    attachment — this is offered inline, because a video nobody can play is
    not much of an archive. What makes that safe is that the type is never
    inferred: `media.content_type` maps a short extension allowlist to one
    fixed value and refuses everything else, and `nosniff` stops the browser
    reconsidering. yt-dlp takes the extension from the remote server, so it is
    exactly the sort of attacker-influenced string that must not choose its
    own content type.

    `FileResponse` also answers Range requests, which is what lets somebody
    seek in a two-hour recording instead of downloading it first.
    """
    site = _require_site(db, site_id)
    try:
        path = media.file_path(settings, site.archive_path, capture_dir, filename)
    except (media.MediaError, StoragePathError):
        raise ApiError("not_found", "No such media file.", status_code=404) from None
    if not path.is_file():
        raise ApiError("not_found", "No such media file.", status_code=404)
    return FileResponse(
        path,
        media_type=media.content_type(filename),
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=300",
            # Archived third-party bytes: never let this page host script or
            # be framed by anything, whatever the browser decides it is.
            "Content-Security-Policy": "default-src 'none'; media-src 'self'; sandbox",
        },
    )


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
