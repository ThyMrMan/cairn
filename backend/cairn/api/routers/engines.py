"""Engines: what is installed, their config schemas, and rescanning (docs/09)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from cairn.api.deps import ClientIp, Csrf, CurrentUser, DbSession
from cairn.api.errors import ApiError
from cairn.api.schemas import EngineSummary
from cairn.engines.registry import EngineConfigError, EngineError, EngineRegistry
from cairn.services import audit

router = APIRouter(tags=["engines"], dependencies=[Csrf])


def _registry(request: Request) -> EngineRegistry:
    registry: EngineRegistry = request.app.state.registry
    return registry


def _availability(engine: Any) -> tuple[bool, str | None]:
    """Whether this engine could run here, and why not if it could not.

    Only the environment, not the manifest — a container engine on a host with
    no Docker socket is a perfectly valid engine that cannot run, and saying
    so in the picker is better than discovering it when a capture fails.
    """
    needs_docker = bool(engine.capabilities.get("requires_docker")) or (
        engine.runtime.get("type") == "docker"
    )
    if needs_docker:
        from cairn.services import containers

        ok, reason = containers.available()
        if not ok:
            return False, reason
    if engine.capabilities.get("requires_browser") and not needs_docker:
        from cairn.services import browser

        ok, reason = browser.availability()
        if not ok:
            return False, reason
    return True, None


@router.get("/engines", response_model=list[EngineSummary])
def list_engines(
    _user: CurrentUser, registry: Annotated[EngineRegistry, Depends(_registry)]
) -> list[EngineSummary]:
    errors = registry.errors
    installed = [
        EngineSummary(
            id=e.id,
            name=e.name,
            version=e.version,
            source=e.source,
            description=e.description,
            capabilities=e.capabilities,
            enabled=True,
            available=_availability(e)[0],
            unavailable_reason=_availability(e)[1],
            error=None,
        )
        for e in registry.all()
    ]
    # Broken drop-ins are listed too. An addon that fails to load and simply
    # does not appear is indistinguishable from one that was never installed.
    installed += [
        EngineSummary(
            id=engine_id,
            name=engine_id,
            version="",
            source="dropin",
            description="",
            capabilities={},
            enabled=False,
            error=message,
        )
        for engine_id, message in errors.items()
    ]
    return installed


@router.get("/engines/{engine_id}/schema")
def engine_schema(
    engine_id: str, _user: CurrentUser, registry: Annotated[EngineRegistry, Depends(_registry)]
) -> dict[str, Any]:
    try:
        engine = registry.get(engine_id)
    except EngineError as exc:
        raise ApiError("not_found", str(exc), status_code=404) from exc
    return {"schema": engine.config_schema, "defaults": engine.defaults()}


@router.post("/engines/{engine_id}/validate")
def validate_config(
    engine_id: str,
    body: dict[str, Any],
    _user: CurrentUser,
    registry: Annotated[EngineRegistry, Depends(_registry)],
) -> dict[str, Any]:
    try:
        engine = registry.get(engine_id)
    except EngineError as exc:
        raise ApiError("not_found", str(exc), status_code=404) from exc
    try:
        return {"ok": True, "config": engine.validate_config(body)}
    except EngineConfigError as exc:
        return {"ok": False, "problems": exc.problems}


@router.post("/engines/rescan", response_model=list[EngineSummary])
def rescan(
    db: DbSession,
    user: CurrentUser,
    ip: ClientIp,
    registry: Annotated[EngineRegistry, Depends(_registry)],
) -> list[EngineSummary]:
    """Re-read /config/engines so a drop-in needs no container restart."""
    registry.refresh(db)
    audit.record(db, audit.ENGINES_RESCAN, actor=user.username, ip=ip)
    return list_engines(user, registry)
