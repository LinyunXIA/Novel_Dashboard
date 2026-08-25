"""holding_event 加 closed_on 结清日（F-P2-03 follow-up）。

apply_merger 对旧公司改「标记结清」而非销毁 shares=0，使分拆/并购前年份的持仓市值
不再漏记；估值按 closed_on 时间窗求值（model/core.py HoldingEvent.closed_on）。

Revision ID: d1e2f3a4b5ca
Revises: d1e2f3a4b5c9
Create Date: 2026-08-25
"""
from alembic import op
import sqlalchemy as sa

revision = 'd1e2f3a4b5ca'
down_revision = 'd1e2f3a4b5c9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("holding_event",
                  sa.Column("closed_on", sa.Date(), nullable=True,
                            comment="结清日；NULL=未结清(open)"))


def downgrade() -> None:
    op.drop_column("holding_event", "closed_on")