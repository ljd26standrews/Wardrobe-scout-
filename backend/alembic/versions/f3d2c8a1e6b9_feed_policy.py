"""feed log: policy, mode, calibration and gate details of each served feed (selective recommendation)

Revision ID: f3d2c8a1e6b9
Revises: e7c1a9d3b2f4
Create Date: 2026-10-21 21:00:00

Additive only: four new columns on feed_requests. Existing rows become policy "ranked" (the feed they were).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f3d2c8a1e6b9"
down_revision: str | Sequence[str] | None = "e7c1a9d3b2f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("feed_requests") as batch:
        batch.add_column(sa.Column("policy", sa.String(20), nullable=False, server_default="ranked"))
        batch.add_column(sa.Column("mode", sa.String(20), nullable=True))
        batch.add_column(sa.Column("calibration_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("gate", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("feed_requests") as batch:
        batch.drop_column("gate")
        batch.drop_column("calibration_id")
        batch.drop_column("mode")
        batch.drop_column("policy")
