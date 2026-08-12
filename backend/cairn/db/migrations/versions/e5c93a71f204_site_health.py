"""site health

Revision ID: e5c93a71f204
Revises: d4a6f0c81b23
Created: 2026-08-11 23:12:04.115220

Whether the *live* site is still there. One row per site, overwritten in
place — this is a current state rather than a history, and the only historical
fact worth keeping is `since`, which is what turns "returning 404" into "has
been returning 404 since March".

Its own table rather than columns on `sites` because it is a different kind of
fact. Everything on `sites` describes the archive; this describes somebody
else's server, checked on a schedule that has nothing to do with capturing, and
absent entirely until the first check runs.

`consecutive` and `pending_state` are what stop one bad minute announcing that
a blog is gone: a state change has to be seen more than once before it is
believed, and until then the reported state is the old one.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import cairn.db.types

revision: str = "e5c93a71f204"
down_revision: str | None = "d4a6f0c81b23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "site_health",
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="live"),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("checked_at", cairn.db.types.UtcDateTime(length=32), nullable=False),
        sa.Column("since", cairn.db.types.UtcDateTime(length=32), nullable=False),
        sa.Column("consecutive", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pending_state", sa.String(length=16), nullable=True),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("site_id"),
    )


def downgrade() -> None:
    op.drop_table("site_health")
