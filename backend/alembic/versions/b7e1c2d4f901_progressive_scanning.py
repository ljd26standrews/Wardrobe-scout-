"""progressive scanning: analysis states, image roles, scan control and stats

Revision ID: b7e1c2d4f901
Revises: 53601a417a9c
Create Date: 2026-10-21 12:00:00

Additive only: new nullable columns (or columns with server defaults) and one index. Existing products
were fully processed by the previous pipeline, so they start in the ``analysed`` state.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7e1c2d4f901"
down_revision: str | Sequence[str] | None = "53601a417a9c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("products") as b:
        b.add_column(sa.Column("analysis_state", sa.String(20), nullable=False, server_default="analysed"))
        b.add_column(sa.Column("detail_fetched_at", sa.DateTime(), nullable=True))
        b.add_column(sa.Column("analysed_at", sa.DateTime(), nullable=True))
        b.add_column(sa.Column("etag", sa.String(200), nullable=True))
        b.add_column(sa.Column("last_modified", sa.String(80), nullable=True))
        b.create_index("ix_products_analysis_state", ["analysis_state"])
    with op.batch_alter_table("product_images") as b:
        b.add_column(sa.Column("thumb_url", sa.String(1500), nullable=True))
        b.add_column(sa.Column("role", sa.String(20), nullable=True))
        b.add_column(sa.Column("role_confidence", sa.Float(), nullable=True))
        b.add_column(sa.Column("selected", sa.Boolean(), nullable=True))
        b.add_column(sa.Column("analysis", sa.JSON(), nullable=True))
        b.create_index("ix_product_images_url", ["url"])
    with op.batch_alter_table("jobs") as b:
        b.add_column(sa.Column("control", sa.String(20), nullable=True))
        b.add_column(sa.Column("stats", sa.JSON(), nullable=True))
    with op.batch_alter_table("scan_runs") as b:
        b.add_column(sa.Column("mode", sa.String(20), nullable=True))
        b.add_column(sa.Column("discovery_complete", sa.Boolean(), nullable=True))
        b.add_column(sa.Column("stats", sa.JSON(), nullable=True))
    # Products scanned before this revision were processed in full; record when.
    op.execute("UPDATE products SET detail_fetched_at = last_seen_at, analysed_at = last_seen_at")


def downgrade() -> None:
    with op.batch_alter_table("scan_runs") as b:
        b.drop_column("stats")
        b.drop_column("discovery_complete")
        b.drop_column("mode")
    with op.batch_alter_table("jobs") as b:
        b.drop_column("stats")
        b.drop_column("control")
    with op.batch_alter_table("product_images") as b:
        b.drop_index("ix_product_images_url")
        for c in ("analysis", "selected", "role_confidence", "role", "thumb_url"):
            b.drop_column(c)
    with op.batch_alter_table("products") as b:
        b.drop_index("ix_products_analysis_state")
        for c in ("last_modified", "etag", "analysed_at", "detail_fetched_at", "analysis_state"):
            b.drop_column(c)
