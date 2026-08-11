"""feed polling state and poll history

Revision ID: c8b4f2913a07
Revises: a3c07b2e5d41
Created: 2026-08-11 17:44:03.882110

M0 shipped `feeds` and `feed_items` as part of the whole schema, but only the
shape a feed needs to *exist*. Polling one needs the shape it needs to *run*:
when it is next due, whether new items should be captured without asking, and
enough on each item to recognise it again when the source rewrites its guids.

`feeds.next_poll_at` is the schedule itself rather than something derived from
`last_polled_at`. Jitter has to be stored somewhere or it is not jitter, and a
stored due time makes "what should run now" one indexed comparison whose answer
does not depend on how long the container was stopped.

`feed_polls` is new and is the point of the milestone as much as the polling
is: every poll is recorded whether or not anything came of it.

All the added columns are either nullable or carry a server_default, because
existing feeds — discovery has been creating them since M2 — must come through
the migration in a state the poller can read.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import cairn.db.types

revision: str = "c8b4f2913a07"
down_revision: str | None = "a3c07b2e5d41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("feeds", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("auto_capture", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch_op.add_column(
            sa.Column(
                "recapture_on_update", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch_op.add_column(
            sa.Column("next_poll_at", cairn.db.types.UtcDateTime(length=32), nullable=True)
        )
        batch_op.add_column(sa.Column("last_status", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("disabled_reason", sa.Text(), nullable=True))
        batch_op.create_index("ix_feeds_due", ["enabled", "next_poll_at"], unique=False)

    with op.batch_alter_table("feed_items", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("canonical_url", sa.Text(), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("updated_at", cairn.db.types.UtcDateTime(length=32), nullable=True)
        )
        # NOT NULL with no sensible constant default: an existing row was last
        # seen when it was first seen, which is per-row rather than fixed. Added
        # nullable, backfilled, then tightened.
        batch_op.add_column(
            sa.Column("last_seen_at", cairn.db.types.UtcDateTime(length=32), nullable=True)
        )
        batch_op.add_column(
            sa.Column("gone_at", cairn.db.types.UtcDateTime(length=32), nullable=True)
        )

    op.execute("UPDATE feed_items SET last_seen_at = first_seen_at WHERE last_seen_at IS NULL")
    op.execute("UPDATE feed_items SET canonical_url = url WHERE canonical_url = ''")

    with op.batch_alter_table("feed_items", schema=None) as batch_op:
        batch_op.alter_column(
            "last_seen_at", existing_type=cairn.db.types.UtcDateTime(length=32), nullable=False
        )
        batch_op.create_index("ix_feed_items_canonical", ["feed_id", "canonical_url"], unique=False)
        batch_op.create_index("ix_feed_items_pending", ["feed_id", "status"], unique=False)

    op.create_table(
        "feed_polls",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("feed_id", sa.Integer(), nullable=False),
        sa.Column("ts", cairn.db.types.UtcDateTime(length=32), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("entries_seen", sa.Integer(), nullable=False),
        sa.Column("new_items", sa.Integer(), nullable=False),
        sa.Column("gone_items", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["feed_id"], ["feeds.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_feed_polls_feed_ts", "feed_polls", ["feed_id", sa.text("ts DESC")])


def downgrade() -> None:
    op.drop_index("ix_feed_polls_feed_ts", table_name="feed_polls")
    op.drop_table("feed_polls")

    with op.batch_alter_table("feed_items", schema=None) as batch_op:
        batch_op.drop_index("ix_feed_items_pending")
        batch_op.drop_index("ix_feed_items_canonical")
        batch_op.drop_column("gone_at")
        batch_op.drop_column("last_seen_at")
        batch_op.drop_column("updated_at")
        batch_op.drop_column("canonical_url")

    with op.batch_alter_table("feeds", schema=None) as batch_op:
        batch_op.drop_index("ix_feeds_due")
        batch_op.drop_column("disabled_reason")
        batch_op.drop_column("last_status")
        batch_op.drop_column("next_poll_at")
        batch_op.drop_column("recapture_on_update")
        batch_op.drop_column("auto_capture")
