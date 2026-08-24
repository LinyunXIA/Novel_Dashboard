"""统一搜索索引表（F-P1-08 · DESIGN §18）。

search_index：条目/行级 embedding（pgvector，维=EMBED_DIM）。

Revision ID: d1e2f3a4b5c7
Revises: d1e2f3a4b5c6
Create Date: 2026-08-24
"""
from alembic import op
import sqlalchemy as sa

revision = 'd1e2f3a4b5c7'
down_revision = 'd1e2f3a4b5c6'
branch_labels = None
depends_on = None

EMBED_DIM = 4096  # Qwen3-Embedding-8B 实测输出 4096 维（须与 config.CONFIG.embed_dim 一致）


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "search_index",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("source_table", sa.Text(), nullable=False),
        sa.Column("source_row_id", sa.BigInteger(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", sa.Text(), nullable=True,
                  comment="pgvector 向量（EMBED_DIM）；建 Text 后 ALTER 成 vector"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("source_table", "source_row_id", "content",
                            name="uq_search_source_row_content"),
    )
    # 真正的 pgvector 列（pgvector 的 Vector 不是标准 DDL type，建 Text 后 ALTER）
    # 注意：ivfflat 上限 2000 维；EMBED_MODEL=4096 维 → 不用 ivfflat，检索走精确余弦扫描（行数小）。
    op.execute(f"ALTER TABLE search_index ALTER COLUMN embedding TYPE vector({EMBED_DIM}) USING embedding::vector({EMBED_DIM})")


def downgrade() -> None:
    op.drop_table("search_index")