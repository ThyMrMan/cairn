"""Engine and session management.

SQLite in WAL mode (docs/00 D5). Every connection gets the same pragmas — the
defaults are wrong for a server workload in ways that only show up under
concurrency:

  journal_mode=WAL   readers do not block the writer
  foreign_keys=ON    OFF by default in SQLite; without this, ON DELETE is a lie
  busy_timeout=5000  wait for a lock instead of raising "database is locked"
  synchronous=NORMAL safe under WAL, far fewer fsyncs than FULL
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, MetaData, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Explicit constraint naming, so Alembic can ALTER them on SQLite (which needs
# batch mode and therefore needs to know the names).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _apply_pragmas(dbapi_connection: Any, _record: Any) -> None:
    if not isinstance(dbapi_connection, sqlite3.Connection):  # pragma: no cover
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        # Keep temp b-trees in memory; the alternative is writing them to
        # whatever TMPDIR points at, which on Unraid may be the array.
        cursor.execute("PRAGMA temp_store=MEMORY")
    finally:
        cursor.close()


def get_engine(url: str, *, echo: bool = False, in_memory: bool = False) -> Engine:
    """Create an engine with the pragmas wired up.

    `in_memory` keeps a single shared connection alive so `:memory:` databases
    survive between sessions — required for tests.
    """
    kwargs: dict[str, Any] = {
        "echo": echo,
        "future": True,
        "connect_args": {"check_same_thread": False},
    }
    if in_memory:
        kwargs["poolclass"] = StaticPool

    engine = create_engine(url, **kwargs)
    event.listen(engine, "connect", _apply_pragmas)
    return engine


def sessionmaker_for(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transactional scope. Commits on success, rolls back on any exception."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
