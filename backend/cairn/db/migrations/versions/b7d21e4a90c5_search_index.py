"""full-text search index

Revision ID: b7d21e4a90c5
Revises: c8b4f2913a07
Created: 2026-08-11 20:10:44.117204

`page_text` holds one row per archived page and no text at all: where the text
is, how long it is, and which capture it came from. The FTS5 table beside it
is `content=''` — contentless, storing terms and no copy of the document.

That choice is measured rather than assumed. Over the same corpus, an
ordinary FTS5 table costs 1.29x the raw text and a contentless one 0.21x, and
this database is copied whole before every migration with ten backups kept —
so the ordinary form would multiply a gigabyte of extracted text into nearly
thirteen gigabytes on the cache pool. The text is derived data that already
exists on the array in `derived/text/`, and a second copy of it is not what
should be in the thing being backed up.

Two consequences the code has to live with, both verified against the shipped
SQLite 3.46.1:

  * `snippet()` and `highlight()` return NULL on a contentless table, so
    result snippets are built from the file on disk.
  * `contentless_delete=1` (SQLite 3.43+) is what makes DELETE possible at
    all, and even with it an UPDATE of a subset of columns is rejected. Rows
    are replaced, never updated.

The virtual table brings four shadow tables with it, and Alembic's
autogenerate sees all five as tables to drop. `include_object` in env.py
filters them out; the drift test does the same.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import cairn.db.types

revision: str = "b7d21e4a90c5"
down_revision: str | None = "c8b4f2913a07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FTS_TABLE = "page_text_fts"
# No stemmer. Porter would find "running" when you typed "run", and the
# snippet is located in the stored text by looking for what you typed — so a
# stemmed hit would come back highlighted nowhere, which reads as broken.
# Prefix search stays available explicitly, with a trailing *.
TOKENIZER = "unicode61 remove_diacritics 2"


def upgrade() -> None:
    op.create_table(
        "page_text",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("capture_id", sa.Integer(), nullable=True),
        sa.Column("capture_dir", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("timestamp", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("offset", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("length", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("words", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("indexed_at", cairn.db.types.UtcDateTime(length=32), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["capture_id"], ["captures.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_id", "url", name="uq_page_text_site_url"),
    )
    op.create_index("ix_page_text_site", "page_text", ["site_id"], unique=False)
    op.create_index("ix_page_text_capture", "page_text", ["capture_id"], unique=False)

    op.execute(
        f"CREATE VIRTUAL TABLE {FTS_TABLE} USING fts5("
        f"title, body, content='', contentless_delete=1, tokenize='{TOKENIZER}')"
    )


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {FTS_TABLE}")
    op.drop_index("ix_page_text_capture", table_name="page_text")
    op.drop_index("ix_page_text_site", table_name="page_text")
    op.drop_table("page_text")
