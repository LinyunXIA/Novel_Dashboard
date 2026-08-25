"""issue #108 回归：snapshot 部分唯一索引的 WHERE 必须可编译为 DDL。

曾因误用类型构造器 Text("...") 当 SQL 片段，create_all() 编译即崩；
迁移侧写法正确所以线上未暴露。此处直接对 postgresql 方言编译断言。
"""
from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from app.model import Snapshot


def _partial_index_names() -> set[str]:
    return {i.name for i in Snapshot.__table__.indexes}


def test_partial_unique_indexes_exist():
    names = _partial_index_names()
    assert {"ux_snap_year", "ux_snap_date"} <= names


def test_partial_index_where_compiles_against_pg_dialect():
    for idx in Snapshot.__table__.indexes:
        if idx.name not in ("ux_snap_year", "ux_snap_date"):
            continue
        ddl = str(CreateIndex(idx).compile(dialect=postgresql.dialect()))
        assert "WHERE" in ddl.upper(), f"{idx.name} 缺少 WHERE 子句"
    # 显式复现旧 bug 场景：两个部分索引都能无异常编译
    compiled = [
        str(CreateIndex(i).compile(dialect=postgresql.dialect()))
        for i in Snapshot.__table__.indexes
        if i.name in ("ux_snap_year", "ux_snap_date")
    ]
    assert len(compiled) == 2
