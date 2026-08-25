"""事件·股票表（F-P2-02 · DESIGN §19.6）。

stock_event：Style A 股票事件导入+不关联 + 同币种 UI 手动关联（entity+account）
→ apply_buy/sell/dividend 实体化 holding_event(batch) + 写 ledger。

Revision ID: d1e2f3a4b5c9
Revises: d1e2f3a4b5c8
Create Date: 2026-08-25
"""
from alembic import op
import sqlalchemy as sa

revision = 'd1e2f3a4b5c9'
down_revision = 'd1e2f3a4b5c8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stock_event",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("company", sa.String(), nullable=False),
        sa.Column("ticker", sa.String()),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("event_type", sa.String(), comment="buy/sell/dividend/pseudo"),
        sa.Column("currency", sa.String()),
        sa.Column("shares", sa.Numeric()),
        sa.Column("unit_price", sa.Numeric()),
        sa.Column("amount", sa.Numeric(), comment="单位：万美金"),
        sa.Column("pct", sa.Numeric()),
        sa.Column("linked_entity_id", sa.BigInteger(), sa.ForeignKey("entity.id")),
        sa.Column("linked_account_id", sa.BigInteger(), sa.ForeignKey("account.id")),
        sa.Column("linked_at", sa.DateTime()),
        sa.Column("source_file", sa.Text()),
        sa.Column("source_line", sa.Integer()),
        sa.Column("version_id", sa.BigInteger()),
        sa.UniqueConstraint("company", "date", "event_type", "source_file",
                            name="uq_stock_company_date_type_source"),
    )


def downgrade() -> None:
    op.drop_table("stock_event")