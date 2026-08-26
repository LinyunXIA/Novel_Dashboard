"""ingest_report 表（issue #118 · §11.4 冲突/失败报告落库）。

冲突命中此前仅 stdout，进程结束即失；落库后供数据调整员回看与
「导入状态」屏展示。Revision ID: e2b3c4d5e6f7
"""
from alembic import op
import sqlalchemy as sa


revision = 'e2b3c4d5e6f7'
down_revision = 'e1a2b3c4d5e6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ingest_report",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("file_path", sa.Text(), nullable=False, comment="源相对路径"),
        sa.Column("rule", sa.Text(), comment="规则名（H1/H2/H4/FX-AUTH…）；解析失败为 NULL"),
        sa.Column("level", sa.String(), nullable=False,
                  comment="block=硬拦截 / warn=软警告 / error=解析失败"),
        sa.Column("line", sa.Integer(), comment="源文件行号"),
        sa.Column("detail", sa.Text(), nullable=False, comment="含新旧值对照的明细"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("level IN ('block','warn','error')", name="ck_ingest_report_level"),
    )
    op.create_index("ix_ingest_report_level_created", "ingest_report", ["level", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_ingest_report_level_created", table_name="ingest_report")
    op.drop_table("ingest_report")
