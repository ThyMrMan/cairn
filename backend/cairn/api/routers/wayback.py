"""Wayback pre-fill: per-site options and the coverage survey (docs/19, stage 1)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field

from cairn.api.deps import AppSettings, ClientIp, Csrf, CurrentUser, DbSession
from cairn.api.errors import ApiError
from cairn.api.schemas import JobAccepted
from cairn.db.models import Site
from cairn.services import audit, wayback
from cairn.services import sites as site_service

router = APIRouter(tags=["wayback"], dependencies=[Csrf])


class UrlGroupModel(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    pattern: str = Field(min_length=1, max_length=1024)
    prefer: list[str] = Field(default_factory=list, max_length=32)
    keep: list[str] = Field(default_factory=list, max_length=32)


class WaybackOptions(BaseModel):
    url_groups: list[UrlGroupModel] = Field(default_factory=list, max_length=32)
    rate_limit_page: str | None = Field(default=None, max_length=1024)
    live_items: int | None = Field(default=None, ge=0)


class SurveyRequest(BaseModel):
    # Re-download the listing instead of reusing pages saved by an earlier run.
    refresh: bool = False


def _supervisor(request: Request) -> Any:
    return request.app.state.supervisor


def _require_site(db: DbSession, site_id: int) -> Site:
    site = site_service.get_site(db, site_id)
    if site is None:
        raise ApiError("not_found", "That site does not exist.", status_code=404)
    return site


@router.get("/sites/{site_id}/wayback")
def get_wayback(
    site_id: int, db: DbSession, settings: AppSettings, _user: CurrentUser
) -> dict[str, Any]:
    """Options, the last survey's report, and the last survey job."""
    return wayback.state(db, settings, _require_site(db, site_id))


@router.put("/sites/{site_id}/wayback/options")
def set_wayback_options(
    site_id: int,
    body: WaybackOptions,
    db: DbSession,
    settings: AppSettings,
    user: CurrentUser,
    ip: ClientIp,
) -> dict[str, Any]:
    site = _require_site(db, site_id)
    try:
        stored = wayback.set_options(
            site,
            url_groups=[g.model_dump() for g in body.url_groups],
            rate_limit_page=body.rate_limit_page,
            live_items=body.live_items,
        )
    except ValueError as exc:
        raise ApiError("invalid_wayback_options", str(exc), status_code=422) from exc
    db.flush()
    audit.record(db, "wayback.options", actor=user.username, target=site.slug, ip=ip, detail=stored)
    return wayback.state(db, settings, site)


@router.post(
    "/sites/{site_id}/wayback/survey",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_survey(
    site_id: int,
    db: DbSession,
    user: CurrentUser,
    ip: ClientIp,
    supervisor: Annotated[Any, Depends(_supervisor)],
    body: SurveyRequest | None = None,
) -> JobAccepted:
    """Queue a survey. It talks only to archive.org, never to the site."""
    site = _require_site(db, site_id)
    if wayback.active_survey(db, site.id) is not None:
        raise ApiError(
            "survey_busy",
            "A Wayback survey of this site is already queued or running.",
            status_code=409,
        )
    refresh = bool(body and body.refresh)
    job = supervisor.enqueue(
        db, job_type=wayback.JOB_TYPE, site_id=site.id, spec={"refresh": refresh}, priority=150
    )
    audit.record(
        db,
        "wayback.survey",
        actor=user.username,
        target=site.slug,
        ip=ip,
        detail={"job_id": job.id, "refresh": refresh},
    )
    db.commit()
    supervisor.notify()
    return JobAccepted(job_id=job.id)
