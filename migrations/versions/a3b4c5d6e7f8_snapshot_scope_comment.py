"""issue #156：snapshot.scope 列注释更正为三段式（模型注释已同步，derived.py issue #12）。

DB 侧初始建表（827c44be1b80）注释仍是旧单段示例 'account:12:BEF' / 'entity:3' /
'family:total'；实际 scope 实现为三段式 account:{id}:{currency} /
entity:{id}:{currency} / family:total。本迁移仅修正注释，不改数据。

revision: a3b4c5d6e7f8
revises: f2b3c4d5e6f7
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a3b4c5d6e7f8"
down_revision = "f2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text(
        "COMMENT ON COLUMN snapshot.scope IS "
        "'account:{id}:{currency} / entity:{id}:{currency} / family:total "
        "(三段式，issue #12/#156)'"
    ))


def downgrade() -> None:
    op.execute(sa.text(
        "COMMENT ON COLUMN snapshot.scope IS "
        "'account:12:BEF' / 'entity:3' / 'family:total'"
    ))
