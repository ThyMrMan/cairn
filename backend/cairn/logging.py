"""Structured logging.

JSON by default (machine-readable in `docker logs`), human-readable when
CAIRN_LOG_JSON=false. Secret material is redacted on the way out — cookie
jars, userscript bodies and Authorization headers must never reach a log
sink (docs/11).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_RESERVED = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
    }
)  # fmt: skip

# Patterns whose *values* must never be logged. Matched against extra-field
# names and against the message body for the obvious inline forms.
_SENSITIVE_KEYS = re.compile(
    r"(password|passwd|secret|token|cookie|session|authorization|totp|api[_-]?key)",
    re.IGNORECASE,
)
_INLINE_SECRET = re.compile(
    r"((?:password|secret|token|cookie|authorization)\s*[=:]\s*)(\S+)",
    re.IGNORECASE,
)
REDACTED = "«redacted»"


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return _INLINE_SECRET.sub(rf"\1{REDACTED}", value)
    return value


class SafeLogger(logging.Logger):
    """A Logger that tolerates reserved key names in `extra`.

    stdlib logging raises KeyError if `extra` contains a key that collides
    with a LogRecord attribute — `name`, `module`, `filename`, `args` and a
    dozen more. That turns an innocuous `extra={"name": folder}` into a crash
    at the call site, which is a trap nobody remembers until production. Rename
    collisions instead of exploding.
    """

    def makeRecord(  # noqa: N802 — overriding a stdlib method; the name is fixed
        self,
        name: str,
        level: int,
        fn: str,
        lno: int,
        msg: object,
        args: Any,
        exc_info: Any,
        func: str | None = None,
        extra: Any = None,
        sinfo: str | None = None,
    ) -> logging.LogRecord:
        if extra:
            extra = {(f"x_{k}" if k in _RESERVED else k): v for k, v in extra.items()}
        return super().makeRecord(name, level, fn, lno, msg, args, exc_info, func, extra, sinfo)


# Must happen before any module calls get_logger(); every cairn module imports
# this one first, so this runs early enough.
logging.setLoggerClass(SafeLogger)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = REDACTED if _SENSITIVE_KEYS.search(key) else redact(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class HumanFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s  %(message)s", "%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        record.msg = redact(record.getMessage())
        record.args = ()
        base = super().format(record)
        extras = {
            k: (REDACTED if _SENSITIVE_KEYS.search(k) else v)
            for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        }
        if extras:
            base += "  " + " ".join(f"{k}={v}" for k, v in extras.items())
        return base


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else HumanFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn installs its own handlers; route everything through ours so the
    # redaction and the JSON envelope apply uniformly.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    # SQLAlchemy is chatty at INFO and its statements can contain hashes.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("alembic").setLevel(logging.INFO)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
