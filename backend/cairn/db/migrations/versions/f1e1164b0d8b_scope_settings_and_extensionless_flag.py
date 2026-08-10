"""scope settings and extensionless flag

Revision ID: f1e1164b0d8b
Revises: 8eed4d930259
Created: 2026-08-10 16:46:54.840524

Two columns M1 needs:

`sites.scope_settings` holds the scope decisions that are not per-host —
limits, robots, politeness. These were briefly kept inside `engine_config`,
which cannot work: engine_config is validated against the engine's own JSON
Schema, and every built-in schema declares `additionalProperties: false`, so
any extra key there fails validation at capture time.

`scope_rules.allow_extensionless` permits extension-less URLs on an
assets-only host. No regex over URLs can tell an extension-less image from an
extension-less page, so it is an explicit per-host decision (docs/04).

Both are added with a server_default. Without one, SQLite cannot add a NOT
NULL column to a table that already has rows — which is every instance except
a fresh one, and therefore exactly the case a dev machine never reproduces.

Autogenerate also proposes dropping and recreating `ix_audit_ts` on every run.
That is a false positive: SQLAlchemy cannot reflect expression indexes on
SQLite, so `ts DESC` looks like a change every time. It is omitted here and
should be omitted from future migrations too.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import cairn.db.types

revision: str = "f1e1164b0d8b"
down_revision: str | None = "8eed4d930259"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("scope_rules", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "allow_extensionless",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )

    with op.batch_alter_table("sites", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "scope_settings",
                cairn.db.types.JsonText(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("sites", schema=None) as batch_op:
        batch_op.drop_column("scope_settings")

    with op.batch_alter_table("scope_rules", schema=None) as batch_op:
        batch_op.drop_column("allow_extensionless")
