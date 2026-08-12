"""annotations

Revision ID: f2a71b3d5c08
Revises: e5c93a71f204
Created: 2026-08-12 00:41:52.309814

Notes and highlights on archived pages, anchored to a **quotation** rather
than to an offset or an element.

docs/13 calls anchoring to replayed content "genuinely hard". Against this
architecture it is not hard, it is unavailable: replay runs on a separate
origin precisely so archived JavaScript cannot reach the app, which means the
app cannot reach into the iframe either — no selection, no injected script, no
coordinates. Giving it either would undo the isolation docs/07 and docs/11
exist to enforce.

So annotations anchor to the extracted text the reader view renders, which is
on this application's own origin and entirely under its control. Text is also
the more durable anchor: an offset into `derived/text/` is invalidated by any
re-extraction, and a later capture of the same page has different offsets
again, so a byte range would orphan every annotation on the archive's first
maintenance pass. A quote with a little context either side survives both, and
when it cannot be found at all the reader says so rather than highlighting the
wrong sentence.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import cairn.db.types

revision: str = "f2a71b3d5c08"
down_revision: str | None = "e5c93a71f204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "annotations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("prefix", sa.Text(), nullable=False, server_default=""),
        sa.Column("suffix", sa.Text(), nullable=False, server_default=""),
        sa.Column("block_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("color", sa.String(length=16), nullable=False, server_default="yellow"),
        sa.Column("created_at", cairn.db.types.UtcDateTime(length=32), nullable=False),
        sa.Column("updated_at", cairn.db.types.UtcDateTime(length=32), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_annotations_page", "annotations", ["site_id", "url"])


def downgrade() -> None:
    op.drop_index("ix_annotations_page", table_name="annotations")
    op.drop_table("annotations")
