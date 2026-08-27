"""#201：图谱节点布局坐标表 graph_node_position（拖拽后刷新锁定）。

新建表：entity_id(FK entity) × scope(person/company/all) 存 SVG viewBox 坐标；
UNIQUE(entity_id, scope) 支持幂等 upsert。

revision: b4c5d6e7f8a9
revises: a3b4c5d6e7f8
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b4c5d6e7f8a9"
down_revision = "a3b4c5d6e7f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "graph_node_position",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("entity_id", sa.BigInteger(), sa.ForeignKey("entity.id"), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("x", sa.Float(), nullable=False),
        sa.Column("y", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("entity_id", "scope", name="uq_graph_pos_entity_scope"),
    )


def downgrade() -> None:
    op.drop_table("graph_node_position")