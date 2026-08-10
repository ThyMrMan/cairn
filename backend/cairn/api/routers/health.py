"""Liveness endpoint.

Unauthenticated, because Unraid's healthcheck and any uptime monitor need it.
It therefore must leak nothing: no paths, no site names, no counts (docs/09).
"""

from __future__ import annotations

import shutil

from fastapi import APIRouter
from sqlalchemy import text

from cairn import __version__
from cairn.api.deps import AppSettings, DbSession
from cairn.api.schemas import HealthResponse
from cairn.services.setup import is_complete

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
def health(db: DbSession, settings: AppSettings) -> HealthResponse:
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:  # pragma: no cover — only on a broken database
        db_ok = False

    try:
        free = shutil.disk_usage(settings.data_dir).free
    except OSError:  # pragma: no cover
        free = None

    return HealthResponse(
        status="ok" if db_ok else "degraded",
        version=__version__,
        db=db_ok,
        # Not sensitive, and the frontend needs it before login to decide
        # between the setup page and the login page.
        setup_complete=is_complete(db) if db_ok else False,
        disk_free_bytes=free,
    )
