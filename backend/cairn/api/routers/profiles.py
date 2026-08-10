"""Access profiles (docs/06, docs/09).

Every response here is metadata. No route returns a cookie value, a jar, or a
userscript body — uploads are write-only and reads return counts, covered
hosts, and a fingerprint so the UI can show that the material changed without
ever transmitting it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from cairn.api.deps import AppSealer, ClientIp, Csrf, CurrentUser, DbSession
from cairn.api.errors import ApiError
from cairn.api.schemas import (
    CookieUploadResponse,
    CoverageResponse,
    Ok,
    ProfileCreate,
    ProfileUpdate,
)
from cairn.db.models import AccessProfile, Site
from cairn.db.types import utcnow
from cairn.services import audit
from cairn.services import profiles as profile_service
from cairn.services import sites as site_service

router = APIRouter(tags=["profiles"], dependencies=[Csrf])

MAX_UPLOAD_BYTES = 1024 * 1024


def _require_profile(db: DbSession, profile_id: int) -> AccessProfile:
    profile = db.get(AccessProfile, profile_id)
    if profile is None:
        raise ApiError("not_found", "That access profile does not exist.", status_code=404)
    return profile


@router.get("/profiles")
def list_profiles(db: DbSession, _user: CurrentUser) -> list[dict[str, Any]]:
    rows = db.scalars(select(AccessProfile).order_by(AccessProfile.name)).all()
    return [profile_service.summary(p) for p in rows]


@router.post("/profiles", status_code=status.HTTP_201_CREATED)
def create_profile(
    body: ProfileCreate, db: DbSession, user: CurrentUser, ip: ClientIp
) -> dict[str, Any]:
    profile = AccessProfile(
        name=body.name,
        mode=body.mode,
        user_agent=body.user_agent,
        hosts=body.hosts,
        verify_url=body.verify_url,
        notes=body.notes,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(profile)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError(
            "duplicate_name", f"A profile named {body.name!r} already exists.", status_code=409
        ) from exc
    audit.record(db, audit.PROFILE_CREATE, actor=user.username, target=profile.name, ip=ip)
    return profile_service.summary(profile)


@router.get("/profiles/{profile_id}")
def get_profile(profile_id: int, db: DbSession, _user: CurrentUser) -> dict[str, Any]:
    return profile_service.summary(_require_profile(db, profile_id))


@router.patch("/profiles/{profile_id}")
def update_profile(
    profile_id: int, body: ProfileUpdate, db: DbSession, user: CurrentUser, ip: ClientIp
) -> dict[str, Any]:
    profile = _require_profile(db, profile_id)
    for field in ("name", "user_agent", "hosts", "verify_url", "notes"):
        value = getattr(body, field)
        if value is not None:
            setattr(profile, field, value)
    profile.updated_at = utcnow()
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError("duplicate_name", "That name is already taken.", status_code=409) from exc
    audit.record(db, audit.PROFILE_UPDATE, actor=user.username, target=profile.name, ip=ip)
    return profile_service.summary(profile)


@router.delete("/profiles/{profile_id}", response_model=Ok)
def delete_profile(profile_id: int, db: DbSession, user: CurrentUser, ip: ClientIp) -> Ok:
    profile = _require_profile(db, profile_id)
    in_use = db.scalars(
        select(Site.title).where(Site.profile_id == profile.id, Site.deleted_at.is_(None))
    ).all()
    if in_use:
        raise ApiError(
            "profile_in_use",
            f"Still used by {len(in_use)} site(s): {', '.join(list(in_use)[:5])}.",
            status_code=409,
        )
    name = profile.name
    db.delete(profile)
    audit.record(db, audit.PROFILE_DELETE, actor=user.username, target=name, ip=ip)
    return Ok()


# ── material ─────────────────────────────────────────────────────────────


@router.put("/profiles/{profile_id}/cookies", response_model=CookieUploadResponse)
async def upload_cookies(
    profile_id: int,
    db: DbSession,
    sealer: AppSealer,
    user: CurrentUser,
    ip: ClientIp,
    file: UploadFile = File(...),
) -> CookieUploadResponse:
    """Upload a Netscape cookies.txt. Write-only; returns the parse report."""
    profile = _require_profile(db, profile_id)

    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ApiError("payload_too_large", "That file is larger than 1 MB.", status_code=413)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    try:
        report = profile_service.store_cookies(db, sealer, profile, text)
    except profile_service.ProfileError as exc:
        # The report is the useful part of a rejection, so return it rather
        # than a bare message: "line 14 has 5 fields" is actionable.
        parsed = profile_service.parse_cookies(text)
        raise ApiError(
            "invalid_cookies", str(exc), status_code=422, detail=parsed.to_dict()
        ) from exc

    audit.record(
        db,
        audit.PROFILE_MATERIAL_WRITE,
        actor=user.username,
        target=profile.name,
        ip=ip,
        detail={"kind": "cookies", "count": len(report.cookies)},
    )
    return CookieUploadResponse(**report.to_dict())


@router.delete("/profiles/{profile_id}/material", response_model=Ok)
def clear_material(profile_id: int, db: DbSession, user: CurrentUser, ip: ClientIp) -> Ok:
    profile = _require_profile(db, profile_id)
    profile_service.clear_material(db, profile)
    audit.record(db, audit.PROFILE_MATERIAL_CLEAR, actor=user.username, target=profile.name, ip=ip)
    return Ok()


@router.get("/profiles/{profile_id}/coverage", response_model=CoverageResponse)
def coverage(
    profile_id: int,
    db: DbSession,
    sealer: AppSealer,
    _user: CurrentUser,
    site_id: int = Query(...),
) -> CoverageResponse:
    """Which of a site's scope hosts this jar actually covers.

    The highest-value check in this area: it converts the most common failure
    — a jar scoped to the wrong domain — from a silent six-hour waste into a
    warning before the capture starts (docs/06).
    """
    profile = _require_profile(db, profile_id)
    site = site_service.get_site(db, site_id)
    if site is None:
        raise ApiError("not_found", "That site does not exist.", status_code=404)

    text = profile_service.load_cookies(sealer, profile)
    if text is None:
        return CoverageResponse(covered={}, warnings=["This profile has no cookies stored yet."])

    report = profile_service.parse_cookies(text)
    scope = site_service.resolved_scope(db, site)
    hosts = [h.host for h in scope.hosts]
    covered = profile_service.coverage(report, hosts)
    return CoverageResponse(
        covered=covered,
        warnings=profile_service.coverage_warnings(covered, site.primary_host),
    )
