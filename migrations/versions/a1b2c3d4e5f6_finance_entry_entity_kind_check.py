"""finance_entry.entity_kind 加 CHECK（DESIGN §5.2：仅 person/company）。

Revision ID: a1b2c3d4e5f6
Revises: 827c44be1b80
Create Date: 2026-08-23
"""
from alembic import op

revision = 'a1b2c3d4e5f6'
down_revision = '827c44be1b80'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 先清存量脏数据（DESIGN §5.2：entity_kind 仅 person/company）；当前无写路径、表为空 → 通常 no-op
    op.execute("DELETE FROM finance_entry WHERE entity_kind NOT IN ('person','company')")
    op.create_check_constraint(
        "ck_finance_entity_kind", "finance_entry", "entity_kind IN ('person','company')")


def downgrade() -> None:
    op.drop_constraint("ck_finance_entity_kind", "finance_entry", type_="check")