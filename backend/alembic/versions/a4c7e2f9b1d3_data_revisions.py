"""data revisions: per-kind counters bumped by triggers, so caches of derived results are keyed by the data

Revision ID: a4c7e2f9b1d3
Revises: f3d2c8a1e6b9
Create Date: 2026-10-22 09:00:00

Additive only: one new table (data_revisions) and triggers on products (content columns only), evidence,
product_relations, feedback, wardrobe_items, inspiration_images, sellers and app_settings. No existing row changes.
Downgrade drops the triggers and the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.services import revisions

revision: str = "a4c7e2f9b1d3"
down_revision: str | Sequence[str] | None = "f3d2c8a1e6b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    for statement in revisions.ddl(bind.dialect.name):
        op.execute(sa.text(statement))


def downgrade() -> None:
    bind = op.get_bind()
    for statement in revisions.drop_ddl(bind.dialect.name):
        op.execute(sa.text(statement))
