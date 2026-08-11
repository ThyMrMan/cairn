"""How an archive changes over time: diffs, and what may be deleted.

Two features, one router, because they answer one question between them —
*is another full capture worth its disk?* The diff says what the last one
changed; retention says what could go if the answer is "not much".
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status

from cairn.api.deps import AppSettings, ClientIp, Csrf, CurrentUser, DbSession
from cairn.api.errors import ApiError
from cairn.api.schemas import JobAccepted, RetentionPolicy
from cairn.db.models import Capture, Site
from cairn.services import audit, diffs, retention
from cairn.services import sites as site_service

router = APIRouter(tags=["changes"], dependencies=[Csrf])


def _supervisor(request: Request) -> Any:
    return request.app.state.supervisor


def _require_site(db: DbSession, site_id: int) -> Site:
    site = site_service.get_site(db, site_id)
    if site is None:
        raise ApiError("not_found", "That site does not exist.", status_code=404)
    return site


def _pair(
    db: DbSession, site: Site, before: int | None, after: int | None
) -> tuple[Capture, Capture]:
    """The two captures to compare, defaulting to the last two.

    Defaulting matters: "what changed?" is asked about the most recent
    recapture far more often than about any particular pair, and making
    somebody choose two ids first turns a glance into an errand.
    """
    from sqlalchemy import select

    captures = list(
        db.scalars(
            select(Capture)
            .where(Capture.site_id == site.id, Capture.status.in_(("ok", "partial")))
            .order_by(Capture.started_at.asc(), Capture.id.asc())
        ).all()
    )
    if len(captures) < 2 and (before is None or after is None):
        raise ApiError(
            "not_enough_captures",
            f"Comparing needs two finished captures of this site; there is {len(captures)}.",
            status_code=409,
        )

    by_id = {c.id: c for c in captures}
    left = by_id.get(before) if before is not None else captures[-2]
    right = by_id.get(after) if after is not None else captures[-1]
    if left is None or right is None:
        raise ApiError("not_found", "That capture is not one of this site's.", status_code=404)
    if left.id == right.id:
        raise ApiError("same_capture", "Those are the same capture.", status_code=400)
    if left.started_at > right.started_at:
        left, right = right, left
    return left, right


@router.get("/sites/{site_id}/diff")
def diff_captures(
    site_id: int,
    db: DbSession,
    settings: AppSettings,
    _user: CurrentUser,
    before: Annotated[int | None, Query()] = None,
    after: Annotated[int | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> dict[str, Any]:
    """Which pages two captures disagree about."""
    site = _require_site(db, site_id)
    left, right = _pair(db, site, before, after)
    result = diffs.compare_captures(settings, site, before=left, after=right, limit=limit)
    payload = result.to_dict()
    payload["before_capture_id"] = left.id
    payload["after_capture_id"] = right.id
    return payload


@router.get("/sites/{site_id}/diff/resources")
def diff_resources(
    site_id: int,
    db: DbSession,
    settings: AppSettings,
    _user: CurrentUser,
    before: Annotated[int | None, Query()] = None,
    after: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    """Assets that arrived, went, or were replaced under the same URL.

    Per capture rather than per page: a CDXJ records what was fetched, not
    which page asked for it.
    """
    site = _require_site(db, site_id)
    left, right = _pair(db, site, before, after)
    changes = diffs.compare_resources(settings, site, before=left, after=right)
    return {
        "before_capture": left.dir_name,
        "after_capture": right.dir_name,
        "resources": [c.to_dict() for c in changes],
    }


@router.get("/sites/{site_id}/diff/page")
def diff_page(
    site_id: int,
    db: DbSession,
    settings: AppSettings,
    _user: CurrentUser,
    url: Annotated[str, Query(min_length=1, max_length=2048)],
    before: Annotated[int | None, Query()] = None,
    after: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    """One page, as two captures saw it."""
    site = _require_site(db, site_id)
    left, right = _pair(db, site, before, after)
    return diffs.compare_page(settings, site, before=left, after=right, url=url).to_dict()


# ── retention ────────────────────────────────────────────────────────────


@router.get("/sites/{site_id}/retention")
def retention_plan(
    site_id: int, db: DbSession, settings: AppSettings, _user: CurrentUser
) -> dict[str, Any]:
    """What retention would delete, and why every survivor survives.

    Always a dry run. Nothing on this path removes anything, including when
    the policy is switched off — which is the state it has to be readable in,
    because the dry run is how somebody decides whether to switch it on.
    """
    site = _require_site(db, site_id)
    return retention.plan(db, settings, site).to_dict()


@router.put("/sites/{site_id}/retention")
def set_retention(
    site_id: int,
    body: RetentionPolicy,
    db: DbSession,
    settings: AppSettings,
    user: CurrentUser,
    ip: ClientIp,
) -> dict[str, Any]:
    site = _require_site(db, site_id)
    scope_settings = dict(site.scope_settings or {})
    scope_settings["retention"] = body.model_dump(exclude_none=True)
    site.scope_settings = scope_settings
    db.flush()
    audit.record(
        db,
        "retention.policy",
        actor=user.username,
        target=site.slug,
        ip=ip,
        detail=scope_settings["retention"],
    )
    return retention.plan(db, settings, site).to_dict()


@router.post(
    "/sites/{site_id}/retention/apply",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def apply_retention(
    site_id: int,
    db: DbSession,
    user: CurrentUser,
    ip: ClientIp,
    supervisor: Annotated[Any, Depends(_supervisor)],
) -> JobAccepted:
    """Delete what the plan says is prunable. Not reversible.

    The plan is recomputed inside the job rather than taken from the request:
    the browser tab that produced it may be minutes old, and a capture that has
    since become the last copy of something must not be deleted because an
    older plan said it could be.
    """
    site = _require_site(db, site_id)
    job = supervisor.enqueue(db, job_type="purge", site_id=site.id, spec={}, priority=200)
    audit.record(
        db,
        "retention.apply",
        actor=user.username,
        target=site.slug,
        ip=ip,
        detail={"job_id": job.id},
    )
    db.commit()
    supervisor.notify()
    return JobAccepted(job_id=job.id)
