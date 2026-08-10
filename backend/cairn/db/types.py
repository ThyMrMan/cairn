"""Custom column types and time helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Dialect, String, Text, TypeDecorator


def utcnow() -> datetime:
    """Timezone-aware current time. The only sanctioned source of 'now'."""
    return datetime.now(UTC)


def to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class UtcDateTime(TypeDecorator[datetime]):
    """Stores tz-aware datetimes as ISO 8601 UTC text.

    SQLite has no native datetime type; SQLAlchemy's DateTime silently drops
    tzinfo on the way in and hands back naive values on the way out, which is
    a reliable source of off-by-hours bugs. Storing explicit ISO-8601-with-Z
    keeps the column human-readable, lexically sortable, and unambiguous —
    and matches the TEXT columns in docs/03.
    """

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return to_iso(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)


class JsonText(TypeDecorator[Any]):
    """JSON stored as TEXT.

    Not SQLAlchemy's JSON type: that one relies on dialect-specific handling,
    and we want the on-disk form to be plain readable text so a human with
    `sqlite3` can inspect it.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return json.dumps(value, separators=(",", ":"), default=str)

    def process_result_value(self, value: str | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
