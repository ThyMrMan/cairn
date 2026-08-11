"""page watchers

Revision ID: d4a6f0c81b23
Revises: b7d21e4a90c5
Created: 2026-08-11 21:04:18.552901

A watched page has no `updated` field to move and no entry list to diff, so
the only honest change signal is that its readable text is no longer the same.
`feed_items.content_hash` holds that hash.

Text rather than markup, measured: three consecutive fetches of one unchanged
post — with a visit counter, a rotating ad slot, a comment count and a
"generated at" stamp in the furniture — produced three different body hashes
and one identical extracted-text hash. Hashing the body would report a change
on every poll forever, which is the same as reporting none.

Nullable, because every existing feed item predates it and none of them needs
one: an RSS entry announces its own changes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4a6f0c81b23"
down_revision: str | None = "b7d21e4a90c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("feed_items") as batch:
        batch.add_column(sa.Column("content_hash", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("feed_items") as batch:
        batch.drop_column("content_hash")
