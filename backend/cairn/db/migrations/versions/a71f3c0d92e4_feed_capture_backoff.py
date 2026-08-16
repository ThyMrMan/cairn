"""feed capture backoff

Revision ID: a71f3c0d92e4
Revises: f2a71b3d5c08
Created: 2026-08-15 10:40:12.884301

The poll side has had backoff since it shipped: a feed that fails to fetch
doubles its interval, caps at a day, and disables itself after ten. The
*capture* side had nothing, and the gap was not academic.

`_dispatch_pending` runs every tick — sixty seconds — and enqueues a capture
for every feed holding a pending item. Items only leave `pending` when a
capture succeeds; a failed one puts them straight back. With nothing to say
"one is already queued" and nothing to say "this keeps failing", a single feed
whose capture could not succeed produced one job every sixty seconds
indefinitely. Reported from a running instance at 105 queued jobs for one
site and still climbing at a job a minute.

Two columns, named to mirror the two the poll side already uses so the
mechanisms read the same:

  capture_failures  consecutive failed captures, reset by any success
  next_capture_at   not before this; NULL means "whenever there is something"
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import cairn.db.types

revision: str = "a71f3c0d92e4"
down_revision: str | None = "f2a71b3d5c08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("feeds") as batch:
        batch.add_column(
            sa.Column("capture_failures", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("next_capture_at", cairn.db.types.UtcDateTime(length=32), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("feeds") as batch:
        batch.drop_column("next_capture_at")
        batch.drop_column("capture_failures")
