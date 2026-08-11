"""The database-backed half of configuration.

`cairn.config.Settings` holds what needs a restart to change — paths, ports,
the master key. Everything a person can change while the container is running
lives in the `settings` table, and this is how it is read.

Values are always real JSON, never NULL: "off" is `{"enabled": false}` or
`false`, never a missing row (docs/03, and the same rule `seed_defaults`
follows). So a caller never has to tell "unset" from "set to nothing".
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.db.models import Setting


def get[T](session: Session, key: str, default: T) -> T | Any:
    row = session.get(Setting, key)
    return default if row is None else row.value


def get_int(session: Session, key: str, default: int) -> int:
    value = get(session, key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def all_settings(session: Session) -> dict[str, Any]:
    return {row.key: row.value for row in session.scalars(select(Setting)).all()}


def put(session: Session, key: str, value: Any) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
    session.flush()
