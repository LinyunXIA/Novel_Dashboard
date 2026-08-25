"""代码卫生批（issue #132）：server_default 补齐 / 注释同步 / 时区统一 / labor 溯源列。

- account.status、finance_entry.source、timeline_event.overlay、
  investment.locked、investment_alloc.is_all：补 server_default（此前仅 Python 侧）；
- 9 处建表漏带 comment 同步（labor_wage_benchmark×4、movie_event×2、stock_event、search_index.embedding）；
- search_index.updated_at TIMESTAMP → TIMESTAMPTZ（与其余表口径一致）；
- labor_cpi_growth 补 source_file 列。

Revision ID: e3c4d5e6f7a8
Revises: e2b3c4d5e6f7
Create Date: 2026-08-26
"""
from alembic import op
import sqlalchemy as sa


revision = 'e3c4d5e6f7a8'
down_revision = 'e2b3c4d5e6f7'
branch_labels = None
depends_on = None

_COMMENTS = (
    ("labor_wage_benchmark", "avg_salary", "全行业人均名义年薪（基准）"),
    ("labor_wage_benchmark", "investment_fin_salary", "投资/金融行业年薪（税前，region 币种）"),
    ("labor_wage_benchmark", "cpi_index", "CPI 定基指数（cpi_base_year=100）"),
    ("labor_wage_benchmark", "cpi_base_year", "CPI 基年（中国系列2013；日本2015）"),
    ("movie_event", "currency", "默认 USD"),
    ("movie_event", "region", "NA/OS/single"),
    ("stock_event", "currency", "默认 USD"),
    ("search_index", "embedding", "pgvector 向量（EMBED_DIM）"),
    ("holding_event", "event_type", "buy/sell/split/acquire-cash/acquire-share/pseudo"),
    ("ingest_report", "file_path", "源相对路径"),
    ("ingest_report", "rule", "规则名（H1/H2/H4/FX-AUTH…）；解析失败为 NULL"),
    ("ingest_report", "level", "block=硬拦截 / warn=软警告 / error=解析失败"),
    ("ingest_report", "line", "源文件行号"),
    ("ingest_report", "detail", "含新旧值对照的明细"),
)


def upgrade() -> None:
    op.alter_column("account", "status",
                    server_default=sa.text("'active'"), existing_type=sa.String(), existing_nullable=False)
    op.alter_column("finance_entry", "source",
                    server_default=sa.text("'file'"), existing_type=sa.String())
    op.alter_column("timeline_event", "overlay",
                    server_default=sa.text("false"), existing_type=sa.Boolean(), existing_nullable=False)
    op.alter_column("investment", "locked",
                    server_default=sa.text("true"), existing_type=sa.Boolean(), existing_nullable=False)
    op.alter_column("investment_alloc", "is_all",
                    server_default=sa.text("false"), existing_type=sa.Boolean(), existing_nullable=False)

    for table, col, comment in _COMMENTS:
        op.execute(f"COMMENT ON COLUMN {table}.{col} IS '{comment}'")

    op.alter_column("search_index", "updated_at",
                    type_=sa.DateTime(timezone=True), existing_type=sa.DateTime(),
                    existing_nullable=True)

    op.add_column("labor_cpi_growth",
                  sa.Column("source_file", sa.String(), nullable=True,
                            comment="来源基准文件"))


def downgrade() -> None:
    op.drop_column("labor_cpi_growth", "source_file")
    op.alter_column("search_index", "updated_at",
                    type_=sa.DateTime(), existing_type=sa.DateTime(timezone=True),
                    existing_nullable=True)
    for table, col, _ in _COMMENTS:
        op.execute(f"COMMENT ON COLUMN {table}.{col} IS NULL")
    op.alter_column("account", "status",
                    server_default=None, existing_type=sa.String(), existing_nullable=False)
    op.alter_column("finance_entry", "source",
                    server_default=None, existing_type=sa.String())
    op.alter_column("timeline_event", "overlay",
                    server_default=None, existing_type=sa.Boolean(), existing_nullable=False)
    op.alter_column("investment", "locked",
                    server_default=None, existing_type=sa.Boolean(), existing_nullable=False)
    op.alter_column("investment_alloc", "is_all",
                    server_default=None, existing_type=sa.Boolean(), existing_nullable=False)
