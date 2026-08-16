"""Access profiles (docs/06, docs/09).

Every response here is metadata. No route returns a cookie value, a jar, or a
userscript body — uploads are write-only and reads return counts, covered
hosts, and a fingerprint so the UI can show that the material changed without
ever transmitting it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Query, Request, UploadFile, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from cairn.api.deps import AppSealer, AppSettings, ClientIp, Csrf, CurrentUser, DbSession
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
def clear_material(
    profile_id: int, db: DbSession, settings: AppSettings, user: CurrentUser, ip: ClientIp
) -> Ok:
    profile = _require_profile(db, profile_id)
    profile_service.clear_material(db, profile, settings)
    audit.record(db, audit.PROFILE_MATERIAL_CLEAR, actor=user.username, target=profile.name, ip=ip)
    return Ok()


@router.put("/profiles/{profile_id}/browser-profile")
async def upload_browser_profile(
    profile_id: int,
    db: DbSession,
    sealer: AppSealer,
    settings: AppSettings,
    user: CurrentUser,
    ip: ClientIp,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """Upload a browsertrix browser-profile tarball. Write-only.

    The one credential this application does not produce. browsertrix will not
    read a cookie jar and ignores a profile built by our Chromium, so the only
    thing it accepts is a tarball from its own `create-login-profile` — run
    against the crawler's own image, where the browser is the same one the
    crawl will use (docs/06).

    Spooled to a file rather than read into memory: these are tens of
    megabytes, and `UploadFile` already spills to disk past a threshold.
    """
    profile = _require_profile(db, profile_id)

    spool = Path(settings.tmp_dir) / f"upload-profile-{profile.id}.tar.gz"
    spool.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        fd = os.open(spool, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > profile_service.MAX_BROWSER_PROFILE_BYTES:
                    raise ApiError(
                        "payload_too_large",
                        f"That file is larger than "
                        f"{profile_service.MAX_BROWSER_PROFILE_BYTES // (1024 * 1024)} MB.",
                        status_code=413,
                    )
                out.write(chunk)

        try:
            meta = profile_service.store_browser_profile(db, sealer, profile, settings, spool)
        except profile_service.ProfileError as exc:
            raise ApiError("invalid_browser_profile", str(exc), status_code=422) from exc
    finally:
        # The plaintext tarball is a live browser session. It does not outlive
        # the request under any exit path, including the two raises above.
        spool.unlink(missing_ok=True)

    audit.record(
        db,
        audit.PROFILE_MATERIAL_WRITE,
        actor=user.username,
        target=profile.name,
        ip=ip,
        detail={"kind": "browser_profile", "bytes": meta["size"]},
    )
    return {"browser_profile": meta, "profile": profile_service.summary(profile)}


@router.delete("/profiles/{profile_id}/browser-profile", response_model=Ok)
def clear_browser_profile(
    profile_id: int, db: DbSession, settings: AppSettings, user: CurrentUser, ip: ClientIp
) -> Ok:
    profile = _require_profile(db, profile_id)
    profile_service.clear_browser_profile(db, profile, settings)
    audit.record(db, audit.PROFILE_MATERIAL_CLEAR, actor=user.username, target=profile.name, ip=ip)
    return Ok()


@router.put("/profiles/{profile_id}/script")
async def upload_script(
    profile_id: int,
    db: DbSession,
    sealer: AppSealer,
    user: CurrentUser,
    ip: ClientIp,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """Upload a `.user.js`. Write-only; returns what was parsed out of it."""
    from cairn.services import userscripts

    profile = _require_profile(db, profile_id)
    raw = await file.read(userscripts.MAX_SCRIPT_BYTES + 1)
    if len(raw) > userscripts.MAX_SCRIPT_BYTES:
        raise ApiError("payload_too_large", "That file is larger than 512 KB.", status_code=413)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    try:
        script = profile_service.store_script(db, sealer, profile, text)
    except userscripts.UserscriptError as exc:
        raise ApiError("invalid_script", str(exc), status_code=422) from exc

    # The parse report is metadata, so it can be kept unsealed and shown
    # without ever unsealing the script itself.
    profile.cookie_meta = {**(profile.cookie_meta or {}), "script": script.to_dict()}
    db.flush()
    audit.record(
        db,
        audit.PROFILE_MATERIAL_WRITE,
        actor=user.username,
        target=profile.name,
        ip=ip,
        detail={"kind": "userscript", "name": script.name},
    )
    return {"script": script.to_dict(), "profile": profile_service.summary(profile)}


@router.post("/profiles/{profile_id}/mint")
async def mint_profile(
    profile_id: int, db: DbSession, sealer: AppSealer, user: CurrentUser, ip: ClientIp
) -> dict[str, Any]:
    """Run the stored userscript and keep the cookies it earns.

    Synchronous rather than a queued job: a mint is one page load with a hard
    ceiling, and the person who pressed the button is waiting to see whether
    their script worked. Watching a job row for something that takes five
    seconds would be worse in every way.
    """
    from cairn.services import browser
    from cairn.services import mint as mint_service

    profile = _require_profile(db, profile_id)
    script_text = profile_service.load_script(sealer, profile)
    if script_text is None:
        raise ApiError("no_script", "This profile has no userscript to run.", status_code=409)
    if not profile.verify_url:
        raise ApiError(
            "no_verify_url",
            "Set a verify URL first — it is the page the script runs against.",
            status_code=409,
        )

    try:
        result = await mint_service.mint(
            script_text=script_text,
            verify_url=profile.verify_url,
            user_agent=profile.user_agent,
        )
    except browser.BrowserUnavailableError as exc:
        raise ApiError("browser_unavailable", str(exc), status_code=503) from exc

    if result.ok and result.cookies_text:
        profile_service.store_cookies(db, sealer, profile, result.cookies_text, mode="userscript")
    profile.last_verified_at = utcnow()
    profile.last_verify_result = "ok" if result.ok else "failed"
    db.flush()

    audit.record(
        db,
        audit.PROFILE_MATERIAL_WRITE,
        actor=user.username,
        target=profile.name,
        ip=ip,
        detail={"kind": "mint", "ok": result.ok, "count": result.cookie_count},
    )
    return {"result": result.to_dict(), "profile": profile_service.summary(profile)}


@router.post("/profiles/{profile_id}/verify")
async def verify_profile(
    profile_id: int, db: DbSession, sealer: AppSealer, user: CurrentUser, ip: ClientIp
) -> dict[str, Any]:
    """Fetch the verify URL with this jar and say what came back.

    Always run this before a multi-hour capture (docs/06). It is one request,
    and the failure it catches — a jar that no longer gets past the gate — is
    otherwise discovered by reading the archive weeks later.

    Deliberately plain HTTP rather than the browser: this has to answer the
    question *wget* will face, and a browser would run the site's JavaScript
    and could get past a gate that wget then will not.
    """
    profile = _require_profile(db, profile_id)
    if not profile.verify_url:
        raise ApiError("no_verify_url", "This profile has no verify URL set.", status_code=409)

    text = profile_service.load_cookies(sealer, profile)
    if text is None:
        raise ApiError("no_cookies", "This profile has no cookies stored yet.", status_code=409)

    outcome = await profile_service.verify(profile, text)
    profile.last_verified_at = utcnow()
    profile.last_verify_result = "ok" if outcome["ok"] else "failed"
    db.flush()
    audit.record(
        db,
        "profile.verify",
        actor=user.username,
        target=profile.name,
        ip=ip,
        detail={"ok": outcome["ok"]},
    )
    return outcome


@router.post("/profiles/{profile_id}/verify-browser-profile")
async def verify_browser_profile(
    profile_id: int,
    request: Request,
    db: DbSession,
    sealer: AppSealer,
    settings: AppSettings,
    user: CurrentUser,
    ip: ClientIp,
) -> dict[str, Any]:
    """Load the verify URL in the crawler's own browser with this profile.

    The counterpart of `/verify` for the material that engine actually reads.
    `/verify` is plain HTTP because it answers what *wget* will face; this one
    starts the real crawler, because a browsertrix profile is a Brave
    user-data-dir that only that browser can decrypt, and any other check
    would be answering confidently about something else.

    Slow — it starts Chromium — so it is a button, not a schedule. It is still
    two orders of magnitude cheaper than the alternative, which is reading a
    finished capture to find out.
    """
    import tempfile

    from cairn.services import profilecheck

    profile = _require_profile(db, profile_id)
    if not profile.verify_url:
        raise ApiError(
            "no_verify_url",
            "Set a verify URL first — it is the page the crawler will be asked to load.",
            status_code=409,
        )
    if not profile_service.has_browser_profile(profile):
        raise ApiError(
            "no_browser_profile",
            "This access profile has no browsertrix browser profile attached.",
            status_code=409,
        )

    engine = request.app.state.registry.get("browsertrix")
    image = str((engine.defaults() or {}).get("image") or "")

    # Unsealed into a directory of its own, which is then handed to the
    # container. The temp root also holds cookie jars for other jobs and there
    # is no reason to mount those into a crawler that cannot read one.
    with tempfile.TemporaryDirectory(dir=settings.tmp_dir, prefix="profilecheck-") as tmp:
        target = Path(tmp) / profile_service.BROWSER_PROFILE_FILE_NAME
        profile_service.write_browser_profile(sealer, profile, settings, target)
        outcome = await profilecheck.check(
            target,
            str(profile.verify_url),
            image=image,
            # Under the configured temp directory, which is inside a mounted
            # volume. The system temp is not, and the sibling container cannot
            # be given a path only this process can see.
            work_root=Path(settings.tmp_dir),
            user_agent=profile.user_agent or "",
        )

    profile.last_verified_at = utcnow()
    profile.last_verify_result = "ok" if outcome.ok else "failed"
    db.flush()
    audit.record(
        db,
        "profile.verify_browser",
        actor=user.username,
        target=profile.name,
        ip=ip,
        detail={"verdict": outcome.verdict},
    )
    return outcome.to_dict()


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
