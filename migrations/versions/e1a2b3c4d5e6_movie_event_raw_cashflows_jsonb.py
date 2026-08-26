"""movie_event.raw_cashflows TEXT→JSONB（issue #109 schema drift 修复）。

模型 movie_event.py 声明 JSONBCompat（PG 上即 JSONB），迁移 d1e2f3a4b5c8
误建为 Text，靠 psycopg 客户端序列化侥幸可用：DB 层失去 JSON 校验与操作符，
且 autogenerate 永久报 type diff。存量行已验证全部为合法 JSON，可直接 USING 转换。

Revision ID: e1a2b3c4d5e6
Revises: d1e2f3a4b5ca
Create Date: 2026-08-26
"""
from alembic import op
import sqlalchemy as sa


revision = 'e1a2b3c4d5e6'
down_revision = 'd1e2f3a4b5ca'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "movie_event", "raw_cashflows",
        type_=sa.dialects.postgresql.JSONB(),
        postgresql_using="raw_cashflows::jsonb",
        existing_nullable=True,
        comment="解析出的全部现金流明细",
    )


def downgrade() -> None:
    # 反向：JSONB→TEXT 无损（text 表示即合法 JSON 文本）
    op.alter_column(
        "movie_event", "raw_cashflows",
        type_=sa.Text(),
        postgresql_using="raw_cashflows::text",
        existing_nullable=True,
        comment="解析出的全部现金流明细",
    )
