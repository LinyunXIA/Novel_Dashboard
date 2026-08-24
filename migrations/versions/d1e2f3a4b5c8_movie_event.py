"""事件·电影表（F-P2-01 · DESIGN §19.6）。

movie_event：电影事件导入+不关联+同币种UI手动关联到账户 → 写 ledger。

Revision ID: d1e2f3a4b5c8
Revises: d1e2f3a4b5c7
Create Date: 2026-08-24
"""
from alembic import op
import sqlalchemy as sa

revision = 'd1e2f3a4b5c8'
down_revision = 'd1e2f3a4b5c7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "movie_event",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("currency", sa.String()),
        sa.Column("region", sa.String()),
        sa.Column("investment_total", sa.Numeric()),
        sa.Column("investment_date", sa.Date()),
        sa.Column("principal_return_date", sa.Date()),
        sa.Column("principal_return_amount", sa.Numeric()),
        sa.Column("dividends_total", sa.Numeric()),
        sa.Column("raw_cashflows", sa.Text(), comment="解析明细（PG JSONB）"),
        sa.Column("linked_account_id", sa.BigInteger(), sa.ForeignKey("account.id")),
        sa.Column("linked_at", sa.DateTime()),
        sa.Column("source_file", sa.Text()),
        sa.Column("source_line", sa.Integer()),
        sa.Column("version_id", sa.BigInteger()),
        sa.UniqueConstraint("title", "source_file", name="uq_movie_title_source"),
    )


def downgrade() -> None:
    op.drop_table("movie_event")