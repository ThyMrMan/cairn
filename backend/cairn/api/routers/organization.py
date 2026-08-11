"""Folders, tags and saved views (docs/09).

One router because the three are one feature from the UI's point of view —
the thing you use to find an archive six months after making it.

Every endpoint that changes a path can end up doing one of two very different
operations, and says which: a rename finishes inside the request, a
cross-filesystem copy becomes a job. `MoveOutcome` is how the client tells
them apart without having to guess from how long the request took.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status

from cairn.api.deps import AppSettings, ClientIp, Csrf, CurrentUser, DbSession
from cairn.api.errors import ApiError
from cairn.api.schemas import (
    FolderCreate,
    FolderNodeModel,
    FolderUpdate,
    MoveOutcome,
    Ok,
    SavedViewCreate,
    SavedViewSummary,
    SavedViewUpdate,
    TagCreate,
    TagSummary,
    TagUpdate,
)
from cairn.db.models import SavedView, Tag
from cairn.services import audit, folders, moves, symlinks
from cairn.services import tags as tag_service
from cairn.services.filters import FilterError, SiteFilter
from cairn.services.storage import CrossDeviceMoveError

router = APIRouter(tags=["organization"], dependencies=[Csrf])


def _supervisor(request: Request) -> Any:
    return request.app.state.supervisor


# ── folders ──────────────────────────────────────────────────────────────


def _node(node: folders.FolderNode) -> FolderNodeModel:
    return FolderNodeModel(
        id=node.folder.id,
        parent_id=node.folder.parent_id,
        name=node.folder.name,
        slug=node.folder.slug,
        path=node.folder.path,
        sort_order=node.folder.sort_order,
        site_count=node.site_count,
        total_site_count=node.total_site_count,
        size_bytes=node.size_bytes,
        total_size_bytes=node.total_size_bytes,
        children=[_node(child) for child in node.children],
    )


@router.get("/folders", response_model=list[FolderNodeModel])
def list_folders(db: DbSession, _user: CurrentUser) -> list[FolderNodeModel]:
    return [_node(node) for node in folders.tree(db)]


@router.post("/folders", response_model=FolderNodeModel, status_code=status.HTTP_201_CREATED)
def create_folder(
    body: FolderCreate, db: DbSession, settings: AppSettings, user: CurrentUser, ip: ClientIp
) -> FolderNodeModel:
    try:
        folder = folders.create_folder(db, settings, name=body.name, parent_id=body.parent_id)
    except folders.FolderError as exc:
        raise ApiError("invalid_folder", str(exc), status_code=400) from exc
    except OSError as exc:
        raise ApiError(
            "storage_error", f"the folder could not be created on disk: {exc}", status_code=500
        ) from exc

    audit.record(db, "folder.create", actor=user.username, target=folder.path, ip=ip)
    return _node(folders.FolderNode(folder=folder))


@router.patch("/folders/{folder_id}", response_model=MoveOutcome)
def update_folder(
    folder_id: int,
    body: FolderUpdate,
    response: Response,
    db: DbSession,
    settings: AppSettings,
    user: CurrentUser,
    ip: ClientIp,
    supervisor: Annotated[Any, Depends(_supervisor)],
) -> MoveOutcome:
    """Rename, reparent, or reorder — the first two move a directory."""
    folder = _require_folder(db, folder_id)

    if body.sort_order is not None:
        folder.sort_order = body.sort_order
        db.flush()

    plan: folders.Relocation | None = None
    spec: dict[str, Any] | None = None
    try:
        if body.name is not None and body.name != folder.name:
            plan = folders.plan_rename(db, settings, folder, name=body.name)
            spec = {"op": "rename-folder", "folder_id": folder.id, "name": body.name}
        elif body.reparent:
            plan = folders.plan_reparent(db, settings, folder, parent_id=body.parent_id)
            spec = {"op": "reparent-folder", "folder_id": folder.id, "parent_id": body.parent_id}
    except folders.FolderError as exc:
        raise ApiError("invalid_folder", str(exc), status_code=400) from exc

    if plan is None:
        return MoveOutcome(status="done", method="noop", path=folder.path)

    try:
        result = moves.relocate(db, settings, plan)
    except CrossDeviceMoveError:
        # The rollback matters: `plan_rename` already wrote the new name onto
        # the row, and committing that without moving the directory would leave
        # the database describing a path that does not exist.
        db.rollback()
        return _queue(db, supervisor, spec or {}, folder.path)
    except moves.SiteBusyError as exc:
        raise ApiError("site_busy", str(exc), status_code=409) from exc
    except (moves.MoveError, OSError) as exc:
        raise ApiError("move_failed", str(exc), status_code=409) from exc

    audit.record(
        db,
        "folder.move",
        actor=user.username,
        target=result.new_path,
        ip=ip,
        detail={"from": result.old_path, "sites": len(result.site_ids)},
    )
    response.status_code = status.HTTP_200_OK
    return MoveOutcome(status="done", method=result.method, path=result.new_path)


@router.delete("/folders/{folder_id}", response_model=Ok)
def delete_folder(
    folder_id: int,
    db: DbSession,
    settings: AppSettings,
    user: CurrentUser,
    ip: ClientIp,
    reassign_to: int | None = Query(default=None),
) -> Ok:
    folder = _require_folder(db, folder_id)
    path = folder.path
    try:
        moved = moves.delete_folder(db, settings, folder, reassign_to=reassign_to)
    except folders.FolderError as exc:
        raise ApiError("folder_not_empty", str(exc), status_code=409) from exc
    except CrossDeviceMoveError as exc:
        raise ApiError(
            "cross_device",
            "Those sites would have to be copied to a different filesystem rather than "
            "moved. Move them yourself first — that runs as a job you can watch — then "
            "delete the folder.",
            status_code=409,
        ) from exc
    except (moves.MoveError, OSError) as exc:
        raise ApiError("move_failed", str(exc), status_code=409) from exc

    audit.record(
        db, "folder.delete", actor=user.username, target=path, ip=ip, detail={"moved": moved}
    )
    return Ok()


def _require_folder(db: DbSession, folder_id: int) -> Any:
    folder = folders.get_folder(db, folder_id)
    if folder is None:
        raise ApiError("not_found", "That folder does not exist.", status_code=404)
    return folder


def _queue(db: DbSession, supervisor: Any, spec: dict[str, Any], path: str) -> MoveOutcome:
    job = supervisor.enqueue(db, job_type="move", site_id=spec.get("site_id"), spec=spec)
    db.commit()
    supervisor.notify()
    return MoveOutcome(status="queued", method="copy", path=path, job_id=job.id)


# ── tags ─────────────────────────────────────────────────────────────────


def _tag(tag: Tag, count: int) -> TagSummary:
    return TagSummary(
        id=tag.id,
        name=tag.name,
        slug=tag.slug,
        color=tag.color,
        description=tag.description,
        site_count=count,
    )


@router.get("/tags", response_model=list[TagSummary])
def list_tags(db: DbSession, _user: CurrentUser) -> list[TagSummary]:
    return [_tag(row.tag, row.site_count) for row in tag_service.usage(db)]


@router.post("/tags", response_model=TagSummary, status_code=status.HTTP_201_CREATED)
def create_tag(body: TagCreate, db: DbSession, user: CurrentUser, ip: ClientIp) -> TagSummary:
    try:
        tag = tag_service.create(db, name=body.name, color=body.color, description=body.description)
    except tag_service.TagError as exc:
        raise ApiError("invalid_tag", str(exc), status_code=400) from exc
    audit.record(db, "tag.create", actor=user.username, target=tag.slug, ip=ip)
    return _tag(tag, 0)


@router.patch("/tags/{tag_id}", response_model=TagSummary)
def update_tag(
    tag_id: int,
    body: TagUpdate,
    db: DbSession,
    settings: AppSettings,
    user: CurrentUser,
    ip: ClientIp,
) -> TagSummary:
    tag = db.get(Tag, tag_id)
    if tag is None:
        raise ApiError("not_found", "That tag does not exist.", status_code=404)
    try:
        tag_service.update(db, tag, name=body.name, color=body.color, description=body.description)
    except tag_service.TagError as exc:
        raise ApiError("invalid_tag", str(exc), status_code=400) from exc

    # The slug follows the name, and the slug is the directory name under
    # /data/by-tag — so a rename moves a directory on the share.
    symlinks.safe_rebuild(db, settings)
    audit.record(db, "tag.update", actor=user.username, target=tag.slug, ip=ip)
    counts = {row.tag.id: row.site_count for row in tag_service.usage(db)}
    return _tag(tag, counts.get(tag.id, 0))


@router.delete("/tags/{tag_id}", response_model=Ok)
def delete_tag(
    tag_id: int, db: DbSession, settings: AppSettings, user: CurrentUser, ip: ClientIp
) -> Ok:
    tag = db.get(Tag, tag_id)
    if tag is None:
        raise ApiError("not_found", "That tag does not exist.", status_code=404)
    slug = tag.slug
    removed = tag_service.delete_tag(db, tag)
    symlinks.safe_rebuild(db, settings)
    audit.record(
        db, "tag.delete", actor=user.username, target=slug, ip=ip, detail={"sites": removed}
    )
    return Ok()


# ── saved views ──────────────────────────────────────────────────────────


def _view(view: SavedView) -> SavedViewSummary:
    site_filter = SiteFilter.from_dict(view.query or {})
    return SavedViewSummary(
        id=view.id,
        name=view.name,
        # Round-tripped through the filter rather than echoed back, so a view
        # stored before a field existed reads as the filter understands it
        # today — and a stored query that no longer parses fails here, once,
        # instead of silently matching everything.
        query=site_filter.to_dict(),
        query_string=site_filter.to_query_string(),
        pinned=view.pinned,
    )


@router.get("/views", response_model=list[SavedViewSummary])
def list_views(db: DbSession, _user: CurrentUser) -> list[SavedViewSummary]:
    from sqlalchemy import select

    rows = db.scalars(select(SavedView).order_by(SavedView.pinned.desc(), SavedView.name)).all()
    out: list[SavedViewSummary] = []
    for view in rows:
        try:
            out.append(_view(view))
        except FilterError:
            continue
    return out


@router.post("/views", response_model=SavedViewSummary, status_code=status.HTTP_201_CREATED)
def create_view(body: SavedViewCreate, db: DbSession, _user: CurrentUser) -> SavedViewSummary:
    from sqlalchemy import select

    try:
        site_filter = SiteFilter.from_dict(body.query)
    except FilterError as exc:
        raise ApiError("invalid_filter", str(exc), status_code=400) from exc
    if db.scalar(select(SavedView.id).where(SavedView.name == body.name)):
        raise ApiError("duplicate", f"A view called {body.name!r} already exists.", status_code=409)

    view = SavedView(name=body.name, query=site_filter.to_dict(), pinned=body.pinned)
    db.add(view)
    db.flush()
    return _view(view)


@router.patch("/views/{view_id}", response_model=SavedViewSummary)
def update_view(
    view_id: int, body: SavedViewUpdate, db: DbSession, _user: CurrentUser
) -> SavedViewSummary:
    view = db.get(SavedView, view_id)
    if view is None:
        raise ApiError("not_found", "That view does not exist.", status_code=404)
    if body.name is not None:
        view.name = body.name
    if body.query is not None:
        try:
            view.query = SiteFilter.from_dict(body.query).to_dict()
        except FilterError as exc:
            raise ApiError("invalid_filter", str(exc), status_code=400) from exc
    if body.pinned is not None:
        view.pinned = body.pinned
    db.flush()
    return _view(view)


@router.delete("/views/{view_id}", response_model=Ok)
def delete_view(view_id: int, db: DbSession, _user: CurrentUser) -> Ok:
    view = db.get(SavedView, view_id)
    if view is None:
        raise ApiError("not_found", "That view does not exist.", status_code=404)
    db.delete(view)
    db.flush()
    return Ok()
