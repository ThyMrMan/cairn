"""capture request

Revision ID: c61d4e2a7b90
Revises: a71f3c0d92e4
Created: 2026-09-16 18:05:41.203117

What a capture was asked to fetch lived only in the spec of the job that ran
it: the URL list a feed or a pasted list handed over, whether to fetch those
and nothing else, the feed items to mark when it finished, the companion pass
it was. A paused capture is continued by a new job, and that job's spec said
only which capture to continue — so a paused feed capture resumed as a crawl
of the whole site, and its items were never marked.

Reading the old job instead is not enough either. Pause leaves its job
finished, clearing finished jobs deletes it, and `captures.job_id` is
`ON DELETE SET NULL`: docs/09 already says a paused capture may be continued
"by which time the job is gone". So the request is kept on the capture.

  request   JSON, the job-spec keys that decide what the capture fetches.
            NULL for captures made before this; a resume falls back to their
            job's spec while the job still exists.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import cairn.db.types

revision: str = "c61d4e2a7b90"
down_revision: str | None = "a71f3c0d92e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("captures") as batch:
        batch.add_column(sa.Column("request", cairn.db.types.JsonText(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("captures") as batch:
        batch.drop_column("request")
