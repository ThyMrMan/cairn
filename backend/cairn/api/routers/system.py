"""System endpoints: version, audit log, storage. Expands in later milestones."""

from __future__ import annotations

import shutil

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from cairn.api.deps import AppSettings, CurrentUser, DbSession
from cairn.api.schemas import AuditEntry, Page, VersionResponse
from cairn.build import build_info
from cairn.db.models import AuditLog
from cairn.services import audit

router = APIRouter(tags=["system"])


@router.get("/version", response_model=VersionResponse)
def version(_user: CurrentUser) -> VersionResponse:
    info = build_info()
    return VersionResponse(
        version=info.version,
        build=info.build,
        built_at=info.built_at,
        label=info.label,
    )


@router.get("/audit", response_model=Page[AuditEntry])
def audit_log(
    db: DbSession,
    _user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
) -> Page[AuditEntry]:
    total = db.scalar(select(func.count(AuditLog.id))) or 0
    rows = audit.recent(db, limit=per_page, offset=(page - 1) * per_page)
    return Page[AuditEntry](
        items=[
            AuditEntry(id=r.id, ts=r.ts, actor=r.actor, action=r.action, target=r.target, ip=r.ip)
            for r in rows
        ],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get("/storage")
def storage(settings: AppSettings, _user: CurrentUser) -> dict[str, object]:
    usage = shutil.disk_usage(settings.data_dir)
    return {
        "data_dir": str(settings.data_dir),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        # Placeholders until M1 populates the sites table.
        "sites": 0,
        "archives_bytes": 0,
    }
