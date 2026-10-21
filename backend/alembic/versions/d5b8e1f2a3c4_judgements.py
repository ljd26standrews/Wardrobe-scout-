"""judgements: your taste labels for measuring the recommender

Revision ID: d5b8e1f2a3c4
Revises: c3a9d2e4f507
Create Date: 2026-10-21 17:00:00

Additive only: one new table. Nothing existing is changed.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d5b8e1f2a3c4"
down_revision: str | Sequence[str] | None = "c3a9d2e4f507"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "judgements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="SET NULL"), nullable=True),
        sa.Column("family_key", sa.String(40), nullable=False),
        sa.Column("taste", sa.Integer(), nullable=False),
        sa.Column("owned_similar", sa.Boolean(), nullable=False),
        sa.Column("too_expensive", sa.Boolean(), nullable=False),
        sa.Column("quality_unclear", sa.Boolean(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("sampled_by", sa.String(20), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_judgements_product_id", "judgements", ["product_id"])
    op.create_index("ix_judgements_family_key", "judgements", ["family_key"])
    op.create_index("ix_judgements_created_at", "judgements", ["created_at"])


def downgrade() -> None:
    op.drop_table("judgements")
