"""calibrations: fitted approval-estimate maps with their diagnostics (selective recommendation)

Revision ID: e7c1a9d3b2f4
Revises: d5b8e1f2a3c4
Create Date: 2026-10-21 19:00:00

Additive only: one new table. Nothing existing is changed.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e7c1a9d3b2f4"
down_revision: str | Sequence[str] | None = "d5b8e1f2a3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "calibrations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("engine_version", sa.String(40), nullable=False),
        sa.Column("method", sa.String(40), nullable=False),
        sa.Column("n_labels", sa.Integer(), nullable=False),
        sa.Column("n_positive", sa.Integer(), nullable=False),
        sa.Column("label_hash", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("diagnostics", sa.JSON(), nullable=False),
    )
    op.create_index("ix_calibrations_created_at", "calibrations", ["created_at"])
    op.create_index("ix_calibrations_active", "calibrations", ["active"])


def downgrade() -> None:
    op.drop_table("calibrations")
