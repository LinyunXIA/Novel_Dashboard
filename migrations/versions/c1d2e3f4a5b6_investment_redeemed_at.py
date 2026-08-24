"""投资 add redeemed_at（issue #82：F-P1-01 赎回按笔防重）。

Investment 加可空 `redeemed_at TIMESTAMPTZ` —— 已赎回标记：
- redeem_investment 开头据此按笔判重（非按年），同年多地区投资互不阻塞；
- GET /investments* 据此暴露 redeemed 供前端置灰。
"""
import sqlalchemy as sa

from alembic import op


revision = 'c1d2e3f4a5b6'
down_revision = 'b1c2d3e4f5a6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('investment', sa.Column('redeemed_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('investment', 'redeemed_at')