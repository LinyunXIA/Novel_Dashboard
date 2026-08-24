"""用工成本基准三表（API② · F-P1-10；DESIGN §13.2）。

labor_wage_benchmark / labor_cpi_growth / labor_tax_benchmark
承载本地基准（工资 10 区、CPI 10 区、税率 12 office），供 labor_cost.py 成本公式查询。

Revision ID: d1e2f3a4b5c6
Revises: c1d2e3f4a5b6
Create Date: 2026-08-24
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'd1e2f3a4b5c6'
down_revision = 'c1d2e3f4a5b6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "labor_wage_benchmark",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("region", sa.String(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("investment_fin_salary", sa.Numeric()),
        sa.Column("avg_salary", sa.Numeric()),
        sa.Column("cpi_index", sa.Numeric()),
        sa.Column("cpi_base_year", sa.Integer()),
        sa.Column("source_file", sa.Text()),
        # 含 currency：比/卢 2002 关池转 EUR 同 region×year 存 BEF/EUR 两行
        sa.UniqueConstraint("region", "year", "currency", name="uq_labor_wage_ryc"),
    )
    op.create_table(
        "labor_cpi_growth",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("region", sa.String(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("wage_growth_pct", sa.Numeric(), comment="名义工资同比增幅%"),
        sa.Column("cpi_pct", sa.Numeric(), comment="CPI 同比通胀%"),
        sa.UniqueConstraint("region", "year", name="uq_labor_cpi_region_year"),
    )
    op.create_table(
        "labor_tax_benchmark",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("office", sa.String(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("formula", sa.String(), nullable=False,
                  comment="成本模型类型：single_pct/single_pct_cap/multi_cap/clamp/headcount/uk_nic/onss/ccss/jp"),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default="{}"),
        sa.Column("source_file", sa.Text()),
        sa.UniqueConstraint("office", "year", name="uq_labor_tax_office_year"),
    )


def downgrade() -> None:
    op.drop_table("labor_tax_benchmark")
    op.drop_table("labor_cpi_growth")
    op.drop_table("labor_wage_benchmark")