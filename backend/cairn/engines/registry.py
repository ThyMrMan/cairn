"""Engine discovery, manifest validation, and config validation (docs/05).

Engines are directories containing `engine.yaml`. Built-ins ship inside the
package; addons drop into `/config/engines/<id>/` and are picked up on startup
or on demand. Core never imports an engine's code — it spawns the command the
manifest names, which is what lets an addon be written in any language.

Config validation happens here and on the server, always. The generated form
applies the schema's constraints in the browser, but a request that skips the
form must not be able to put `--limit-rate=$(anything)` into a subprocess
argument list.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

import cairn
from cairn.config import Settings
from cairn.db.models import EngineRecord
from cairn.db.types import utcnow
from cairn.engines.protocol import PROTOCOL_VERSION
from cairn.logging import get_logger
from cairn.services.storage import resolve_within

log = get_logger(__name__)

MANIFEST_FILE = "engine.yaml"
BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"

RUNTIME_TYPES = frozenset({"subprocess", "docker"})
_REQUIRED_KEYS = ("apiVersion", "id", "name", "version", "runtime")


class EngineError(RuntimeError):
    """An engine manifest is unusable, or a config does not satisfy it."""


class EngineConfigError(EngineError):
    """A submitted config failed its engine's JSON Schema."""

    def __init__(self, message: str, problems: list[str]) -> None:
        super().__init__(message)
        self.problems = problems


@dataclass(slots=True)
class Engine:
    """A loaded, validated engine manifest."""

    id: str
    name: str
    version: str
    source: str  # builtin | dropin
    path: Path
    manifest: dict[str, Any]

    @property
    def runtime(self) -> dict[str, Any]:
        runtime: dict[str, Any] = self.manifest.get("runtime") or {}
        return runtime

    @property
    def capabilities(self) -> dict[str, Any]:
        caps: dict[str, Any] = self.manifest.get("capabilities") or {}
        return caps

    @property
    def config_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = self.manifest.get("config_schema") or {"type": "object"}
        return schema

    @property
    def description(self) -> str:
        return str(self.manifest.get("description") or "")

    @property
    def command(self) -> list[str]:
        cmd = self.runtime.get("command") or []
        if not isinstance(cmd, list) or not all(isinstance(c, str) for c in cmd):
            raise EngineError(f"engine {self.id}: runtime.command must be a list of strings")
        argv = [str(c) for c in cmd]

        # A built-in engine runs inside this application's own environment, so
        # a bare `python` on PATH is the wrong interpreter whenever one exists
        # outside the venv — and no interpreter at all on a Windows dev box.
        # Drop-ins keep whatever they wrote: an addon naming `python` means
        # the system one, and silently redirecting it into our venv would be a
        # surprising and undocumented behaviour.
        if self.source == "builtin" and argv and argv[0] in ("python", "python3"):
            argv[0] = sys.executable
        return argv

    def env_overrides(self) -> dict[str, str]:
        """Environment additions for this engine's subprocess.

        A built-in engine is part of core and imports `cairn`, so it needs the
        package importable in a fresh interpreter. Relying on the install is
        not enough: an editable install, a source checkout, or a PYTHONPATH-based
        setup all leave `python -m cairn.engines.wget` failing with
        ModuleNotFoundError — at capture time, from a subprocess, where the
        error is least visible. Core knows where its own package lives, so it
        says so.

        Drop-ins get nothing: injecting our import path into third-party code
        could shadow their modules, and an addon has no reason to import cairn.
        """
        if self.source != "builtin":
            return {}
        package_parent = str(Path(cairn.__file__).resolve().parent.parent)
        existing = os.environ.get("PYTHONPATH", "")
        joined = f"{package_parent}{os.pathsep}{existing}" if existing else package_parent
        return {"PYTHONPATH": joined}

    def defaults(self) -> dict[str, Any]:
        """Every property's `default` from the schema.

        The engine gets a fully populated config even when the site was
        created before a property existed, so an engine can read
        `config["tries"]` without defending against absence.
        """
        props = self.config_schema.get("properties") or {}
        return {
            key: spec["default"]
            for key, spec in props.items()
            if isinstance(spec, dict) and "default" in spec
        }

    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Merge over defaults and validate, or raise EngineConfigError."""
        merged = {**self.defaults(), **(config or {})}
        validator = jsonschema.Draft202012Validator(self.config_schema)
        problems = [
            f"{'.'.join(str(p) for p in err.path) or '(root)'}: {err.message}"
            for err in sorted(validator.iter_errors(merged), key=lambda e: list(e.path))
        ]
        if problems:
            raise EngineConfigError(f"configuration is not valid for engine {self.id}", problems)
        return merged


# ── loading ──────────────────────────────────────────────────────────────


def load_manifest(directory: Path, *, source: str) -> Engine:
    path = directory / MANIFEST_FILE
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EngineError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise EngineError(f"{path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise EngineError(f"{path} must contain a mapping")

    missing = [k for k in _REQUIRED_KEYS if not raw.get(k)]
    if missing:
        raise EngineError(f"{path} is missing required keys: {', '.join(missing)}")

    api_version = str(raw["apiVersion"])
    if api_version != PROTOCOL_VERSION:
        raise EngineError(
            f"{path} declares apiVersion {api_version!r}; this build speaks {PROTOCOL_VERSION!r}"
        )

    engine_id = str(raw["id"])
    if engine_id != directory.name:
        # The directory name is what the operator sees and what the UI links
        # to. Letting them differ makes "which directory is this engine in?"
        # unanswerable from the UI.
        raise EngineError(
            f"{path}: id {engine_id!r} does not match its directory name {directory.name!r}"
        )

    runtime = raw.get("runtime") or {}
    rtype = str(runtime.get("type", ""))
    if rtype not in RUNTIME_TYPES:
        raise EngineError(
            f"{path}: runtime.type must be one of {sorted(RUNTIME_TYPES)}, got {rtype!r}"
        )
    if rtype == "subprocess" and not runtime.get("command"):
        raise EngineError(f"{path}: subprocess runtime needs a command")
    if rtype == "docker" and not runtime.get("image"):
        raise EngineError(f"{path}: docker runtime needs an image")

    schema = raw.get("config_schema")
    if schema is not None:
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError as exc:
            raise EngineError(f"{path}: config_schema is not a valid JSON Schema: {exc}") from exc

    engine = Engine(
        id=engine_id,
        name=str(raw["name"]),
        version=str(raw["version"]),
        source=source,
        path=directory,
        manifest=raw,
    )
    if rtype == "subprocess":
        engine.command  # noqa: B018 — validates the command's shape, raising if wrong
    return engine


def discover(settings: Settings) -> tuple[dict[str, Engine], dict[str, str]]:
    """Find every usable engine, plus the errors for the ones that aren't.

    A broken drop-in must not stop the others from loading, or one bad addon
    takes the whole instance down. Failures are returned so the UI can show
    them next to the engine that produced them.
    """
    engines: dict[str, Engine] = {}
    errors: dict[str, str] = {}

    for source, root in (("builtin", BUILTIN_DIR), ("dropin", settings.engines_dir)):
        if not root.is_dir():
            continue
        for directory in sorted(p for p in root.iterdir() if p.is_dir()):
            if not (directory / MANIFEST_FILE).is_file():
                continue
            try:
                engine = load_manifest(directory, source=source)
            except EngineError as exc:
                errors[directory.name] = str(exc)
                log.warning("engine failed to load", extra={"dir": str(directory), "err": str(exc)})
                continue
            if engine.id in engines and source == "dropin":
                # A drop-in shadowing a built-in is a supported way to pin a
                # patched version, but it should never be a surprise.
                log.warning("drop-in engine overrides a built-in", extra={"engine": engine.id})
            engines[engine.id] = engine

    return engines, errors


def sync_to_db(session: Session, engines: dict[str, Engine], errors: dict[str, str]) -> None:
    """Reconcile the `engines` table with what is actually on disk.

    Rows for engines that vanished are kept but disabled — a site still
    references the engine id, and deleting the row would leave that site
    pointing at nothing with no explanation.
    """
    existing = {row.id: row for row in session.scalars(select(EngineRecord)).all()}

    for engine in engines.values():
        row = existing.get(engine.id)
        if row is None:
            session.add(
                EngineRecord(
                    id=engine.id,
                    name=engine.name,
                    version=engine.version,
                    source=engine.source,
                    manifest=engine.manifest,
                    enabled=True,
                    installed_at=utcnow(),
                    last_error=None,
                )
            )
        else:
            row.name = engine.name
            row.version = engine.version
            row.source = engine.source
            row.manifest = engine.manifest
            row.last_error = None

    for engine_id, row in existing.items():
        if engine_id not in engines:
            row.enabled = False
            row.last_error = errors.get(engine_id) or "engine directory is no longer present"


class EngineRegistry:
    """Process-wide view of the installed engines.

    Held on app state and refreshed by `POST /api/engines/rescan`, so adding a
    drop-in does not need a container restart.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engines: dict[str, Engine] = {}
        self._errors: dict[str, str] = {}

    def refresh(self, session: Session | None = None) -> None:
        self._engines, self._errors = discover(self._settings)
        if session is not None:
            sync_to_db(session, self._engines, self._errors)
        log.info(
            "engines loaded",
            extra={"count": len(self._engines), "failed": len(self._errors)},
        )

    @property
    def errors(self) -> dict[str, str]:
        return dict(self._errors)

    def all(self) -> list[Engine]:
        return sorted(self._engines.values(), key=lambda e: e.id)

    def get(self, engine_id: str) -> Engine:
        engine = self._engines.get(engine_id)
        if engine is None:
            known = ", ".join(sorted(self._engines)) or "none"
            raise EngineError(f"unknown engine {engine_id!r} (installed: {known})")
        return engine

    def resolve_dropin(self, engine_id: str) -> Path:
        """Path of a drop-in engine, guarded against directory traversal."""
        return resolve_within(self._settings.engines_dir, engine_id)
