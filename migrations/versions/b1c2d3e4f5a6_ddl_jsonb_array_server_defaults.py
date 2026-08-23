"""DDL 类型对齐（DESIGN §5.2 + issue #21）。

JSONB/ARRAY/时间戳 server_default 落地：
1. entity.fields:          JSON  → JSONB
2. user_data_overlay.payload: JSON  → JSONB
3. notification.payload:   JSON  → JSONB
4. recompute_job.files:    JSON  → TEXT[]  (DESIGN §5.2 DDL 是 TEXT[])
5. 时间戳列补 server_default=now()：
   - source_file_version.captured_at
   - recompute_job.created_at
   - user_data_overlay.updated_at  (关键：连 Python default 都没有，INSERT 必炸 NOT NULL)
   - notification.created_at

downgrade 镜像反向：JSONB → JSON、ARRAY(Text) → JSON、移除 server_default。
files 字段的 ARRAY↔JSON 转换用 jsonb_array_elements_text 还原数组。
"""
from alembic import op


revision = 'b1c2d3e4f5a6'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- JSON → JSONB ----
    op.execute("ALTER TABLE entity ALTER COLUMN fields TYPE JSONB USING fields::jsonb")
    op.execute("ALTER TABLE user_data_overlay ALTER COLUMN payload TYPE JSONB USING payload::jsonb")
    op.execute("ALTER TABLE notification ALTER COLUMN payload TYPE JSONB USING payload::jsonb")

    # ---- JSON → TEXT[]（两步：Postgres ALTER COLUMN TYPE USING 表达式不允许子查询）----
    # Postgres USING 子句只能用本行其他列的表达式，不允许 SELECT 子查询。
    # 改两步：①ADD COLUMN files_arr TEXT[]；②UPDATE ... ARRAY(jsonb_array_elements_text)；
    # ③DROP COLUMN files；④RENAME files_arr → files。
    op.execute("ALTER TABLE recompute_job ADD COLUMN files_arr TEXT[]")
    # 数组型：jsonb_array_elements_text 展开
    op.execute("""
        UPDATE recompute_job SET files_arr = ARRAY(
            SELECT jsonb_array_elements_text(files::jsonb)
        )
        WHERE files IS NOT NULL AND jsonb_typeof(files::jsonb) = 'array'
    """)
    # 标量/对象型：理论上不应出现，保守包成单元素数组不丢数据
    op.execute("""
        UPDATE recompute_job SET files_arr = ARRAY[files::text]
        WHERE files IS NOT NULL
          AND jsonb_typeof(files::jsonb) IS DISTINCT FROM 'array'
    """)
    op.execute("ALTER TABLE recompute_job DROP COLUMN files")
    op.execute("ALTER TABLE recompute_job RENAME COLUMN files_arr TO files")

    # ---- 时间戳列 server_default ----
    # user_data_overlay.updated_at 必须显式设置（NOT NULL 无 default → INSERT 炸）
    op.execute("ALTER TABLE user_data_overlay ALTER COLUMN updated_at SET DEFAULT now()")
    op.execute("ALTER TABLE source_file_version ALTER COLUMN captured_at SET DEFAULT now()")
    op.execute("ALTER TABLE recompute_job ALTER COLUMN created_at SET DEFAULT now()")
    op.execute("ALTER TABLE notification ALTER COLUMN created_at SET DEFAULT now()")


def downgrade() -> None:
    # ---- 移除 server_default ----
    op.execute("ALTER TABLE notification ALTER COLUMN created_at DROP DEFAULT")
    op.execute("ALTER TABLE recompute_job ALTER COLUMN created_at DROP DEFAULT")
    op.execute("ALTER TABLE source_file_version ALTER COLUMN captured_at DROP DEFAULT")
    op.execute("ALTER TABLE user_data_overlay ALTER COLUMN updated_at DROP DEFAULT")

    # ---- TEXT[] → JSON（同样两步：USING 不能子查询；先用 to_jsonb 把数组 JSON 化）----
    # ARRAY['a','b'] → jsonb 数组 '[a,b]'，再 DROP+RENAME
    op.execute("ALTER TABLE recompute_job ADD COLUMN files_json JSON")
    op.execute("""
        UPDATE recompute_job SET files_json = to_jsonb(files)::jsonb
        WHERE files IS NOT NULL
    """)
    op.execute("ALTER TABLE recompute_job DROP COLUMN files")
    op.execute("ALTER TABLE recompute_job RENAME COLUMN files_json TO files")

    # ---- JSONB → JSON ----
    op.execute("ALTER TABLE notification ALTER COLUMN payload TYPE JSON USING payload::jsonb")
    op.execute("ALTER TABLE user_data_overlay ALTER COLUMN payload TYPE JSON USING payload::jsonb")
    op.execute("ALTER TABLE entity ALTER COLUMN fields TYPE JSON USING fields::jsonb")