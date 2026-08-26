"""linked_at 列统一 TIMESTAMPTZ（issue #145 · 承 #132 时区口径）。

revision: f2b3c4d5e6f7
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f2b3c4d5e6f7"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("movie_event", "linked_at",
                    type_=sa.DateTime(timezone=True), existing_type=sa.DateTime())
    op.alter_column("stock_event", "linked_at",
                    type_=sa.DateTime(timezone=True), existing_type=sa.DateTime())


def downgrade() -> None:
    op.alter_column("movie_event", "linked_at",
                    type_=sa.DateTime(), existing_type=sa.DateTime(timezone=True))
    op.alter_column("stock_event", "linked_at",
                    type_=sa.DateTime(), existing_type=sa.DateTime(timezone=True))
