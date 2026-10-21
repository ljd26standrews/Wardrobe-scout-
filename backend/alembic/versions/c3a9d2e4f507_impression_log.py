"""impression log: feeds served and products displayed (for evaluation)

Revision ID: c3a9d2e4f507
Revises: b7e1c2d4f901
Create Date: 2026-10-21 15:00:00

Additive only: two new tables. Nothing existing is changed.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3a9d2e4f507"
down_revision: str | Sequence[str] | None = "b7e1c2d4f901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feed_requests",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("engine", sa.String(20), nullable=False),
        sa.Column("engine_version", sa.String(40), nullable=False),
        sa.Column("decisions", sa.Integer(), nullable=False),
        sa.Column("candidates", sa.Integer(), nullable=False),
        sa.Column("exploration_rate", sa.Float(), nullable=False),
        sa.Column("filters", sa.JSON(), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
    )
    op.create_index("ix_feed_requests_created_at", "feed_requests", ["created_at"])
    op.create_table(
        "impressions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("feed_id", sa.String(32), sa.ForeignKey("feed_requests.id", ondelete="SET NULL"), nullable=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="SET NULL"), nullable=True),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("surface", sa.String(20), nullable=False),
        sa.Column("explore", sa.Boolean(), nullable=False),
        sa.Column("shown_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_impressions_feed_id", "impressions", ["feed_id"])
    op.create_index("ix_impressions_product_id", "impressions", ["product_id"])
    op.create_index("ix_impressions_shown_at", "impressions", ["shown_at"])


def downgrade() -> None:
    op.drop_table("impressions")
    op.drop_table("feed_requests")
