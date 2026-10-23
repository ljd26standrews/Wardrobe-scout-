"""exposure events: what counts as having seen a piece is versioned, and each event is recorded once

Revision ID: b9e4f1a7c2d5
Revises: a4c7e2f9b1d3
Create Date: 2026-10-22 13:00:00

Additive only: three nullable columns on impressions (definition, event_id, dwell_ms) and a unique index on
event_id. Existing rows keep definition NULL: they recorded rendered tiles, visible or not, and are reported as legacy,
never used as the denominator of an exposure rate (services/exposure.py). Downgrade drops the index and the columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b9e4f1a7c2d5"
down_revision: str | Sequence[str] | None = "a4c7e2f9b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("impressions") as batch:
        batch.add_column(sa.Column("definition", sa.String(20), nullable=True))
        batch.add_column(sa.Column("event_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("dwell_ms", sa.Integer(), nullable=True))
    op.create_index("ix_impressions_event_id", "impressions", ["event_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_impressions_event_id", table_name="impressions")
    with op.batch_alter_table("impressions") as batch:
        batch.drop_column("dwell_ms")
        batch.drop_column("event_id")
        batch.drop_column("definition")
