"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import Response

from cairn import __version__
from cairn.api import errors
from cairn.api.middleware import SecurityHeadersMiddleware
from cairn.api.routers import auth as auth_router
from cairn.api.routers import health as health_router
from cairn.api.routers import setup as setup_router
from cairn.api.routers import system as system_router
from cairn.config import Settings, get_settings, load_or_create_secret_key
from cairn.crypto.sealing import Sealer
from cairn.db.base import get_engine, sessionmaker_for
from cairn.db.bootstrap import (
    ensure_directories,
    run_migrations,
    seed_defaults,
    sweep_tmp,
    verify_secret_key,
    warn_about_environment,
)
from cairn.logging import configure_logging, get_logger
from cairn.services.auth import purge_expired_sessions

log = get_logger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    ensure_directories(settings)
    sweep_tmp(settings)
    run_migrations(settings)

    engine = get_engine(settings.db_url, echo=False)
    factory = sessionmaker_for(engine)
    master_key = load_or_create_secret_key(settings)
    sealer = Sealer(master_key)

    with factory() as session:
        # Fatal if the key changed: sealed data would be silently unreadable.
        verify_secret_key(session, master_key)
        seed_defaults(session)
        purged = purge_expired_sessions(session)
        session.commit()
    if purged:
        log.info("purged expired sessions", extra={"count": purged})

    warn_about_environment(settings, engine)

    app.state.engine = engine
    app.state.sessionmaker = factory
    app.state.sealer = sealer

    log.info(
        "cairn started",
        extra={"version": __version__, "port": settings.port, "data_dir": str(settings.data_dir)},
    )
    try:
        yield
    finally:
        engine.dispose()
        log.info("cairn stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    app = FastAPI(
        title="Cairn",
        version=__version__,
        description="Self-hosted website archiver.",
        lifespan=lifespan,
        openapi_url="/api/openapi.json",
        docs_url="/api/docs" if settings.dev_mode else None,
        redoc_url=None,
    )
    app.state.settings = settings

    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    errors.install_handlers(app)

    app.include_router(health_router.router, prefix="/api")
    app.include_router(setup_router.router, prefix="/api")
    app.include_router(auth_router.router, prefix="/api")
    app.include_router(system_router.router, prefix="/api")

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built SPA, with history fallback.

    Assets are fingerprinted by Vite so they cache forever; index.html must
    not, or a deploy leaves clients pinned to a stale bundle.
    """
    assets = STATIC_DIR / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    index = STATIC_DIR / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str, request: Request) -> Response:
        # /api is handled by routers above; anything reaching here is a miss.
        if full_path.startswith("api/"):
            return JSONResponse(
                status_code=404, content={"error": {"code": "not_found", "message": "Not found."}}
            )

        candidate = (STATIC_DIR / full_path).resolve() if full_path else None
        if (
            candidate is not None
            and candidate.is_file()
            and candidate.is_relative_to(STATIC_DIR.resolve())
        ):
            return FileResponse(candidate)

        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-store"})

        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "frontend_missing",
                    "message": (
                        "The web UI has not been built. Run `npm run build` in frontend/, "
                        "or use the Docker image which builds it for you."
                    ),
                }
            },
        )


app = create_app  # uvicorn --factory cairn.app:app
