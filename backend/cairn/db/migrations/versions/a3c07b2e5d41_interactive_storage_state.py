"""interactive profile storage state

Revision ID: a3c07b2e5d41
Revises: f1e1164b0d8b
Created: 2026-08-11 15:10:22.114903

`access_profiles.storage_enc` holds the full browser storage_state saved by an
interactive session — cookies plus localStorage per origin — sealed like the
rest of the profile material.

The cookies stay in `cookies_enc` as well, and that duplication is deliberate.
wget can only be handed cookies, so that is the column the capture path reads;
the storage state is what lets the same profile drive a browser engine later
(docs/06), and a login that keeps its token in localStorage cannot be
reconstructed from cookies alone.

Nullable, so no server_default is needed: every existing profile predates
interactive mode and correctly has nothing here.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3c07b2e5d41"
down_revision: str | None = "f1e1164b0d8b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("access_profiles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("storage_enc", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("access_profiles", schema=None) as batch_op:
        batch_op.drop_column("storage_enc")
