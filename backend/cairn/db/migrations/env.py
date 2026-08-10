"""Alembic environment.

The database URL comes from Settings, never from alembic.ini, so migrations
always target the same database the app does.

`render_as_batch` is essential on SQLite: it has no real ALTER TABLE, so
Alembic emulates one by creating a new table, copying rows, and swapping.
Without it, any future column drop or type change simply fails.
"""

from __future__ import annotations

from logging.config import fileConfig
from pathlib import Path

from alembic import context

from cairn.config import get_settings
from cairn.db.base import Base, get_engine
from cairn.db.models import *  # populate Base.metadata

config = context.config

# Only take over logging when Alembic is driven from the command line. When
# the app calls command.upgrade() at startup it passes configure_logger=False,
# because fileConfig() replaces the root handlers and every subsequent
# application log would lose its JSON envelope and redaction.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Honour a URL the caller already set. bootstrap.run_migrations() passes the
# active Settings' URL, which may not be the process-wide cached one — tests
# and any multi-config tooling depend on that. Overwriting it here would
# silently migrate a different database than the one the caller meant, and in
# production the two coincide, so the mistake would never surface.
DB_URL = config.get_main_option("sqlalchemy.url") or get_settings().db_url
config.set_main_option("sqlalchemy.url", DB_URL)


def run_migrations_offline() -> None:
    context.configure(
        url=DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    db_file = Path(DB_URL.split("///", 1)[-1])
    db_file.parent.mkdir(parents=True, exist_ok=True)

    # Reuse the app's engine factory rather than engine_from_config: it
    # attaches the pragmas on `connect`, so they apply without issuing a
    # statement here. Running `PRAGMA foreign_keys=ON` against the connection
    # directly opens an implicit transaction that swallows Alembic's commit,
    # leaving alembic_version unwritten and every migration re-running forever.
    connectable = get_engine(DB_URL)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
