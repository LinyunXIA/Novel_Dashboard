"""七轮审计 #186：PG-only 生产路径冒烟测试（SQLite 套件无法覆盖的面）。

- writer.import_return_curves 的 postgresql.insert on_conflict_do_nothing（PG 专用语法）
- search.retrieve 的 pgvector cosine_distance 查询

连接不上 novel_test（本机 Postgres.app）时整文件 skip——CI/无 PG 环境不红。
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_PG_SMOKE") == "1",
    reason="SKIP_PG_SMOKE=1",
)


def _pg_available() -> bool:
    try:
        from sqlalchemy import create_engine, text
        # 七轮 #186：缺省复用 app.config._dsn（postgresql+psycopg 驱动 + 环境变量凭据）
        from app.config import _dsn
        dsn = os.environ.get("PG_TEST_DSN", _dsn("novel_test"))
        eng = create_engine(dsn)
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


pg_ready = pytest.mark.skipif(not _pg_available(), reason="novel_test 不可连（无本地 PG）")


@pytest.fixture
def pg_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.config import _dsn
    dsn = os.environ.get("PG_TEST_DSN", _dsn("novel_test"))
    engine = create_engine(dsn)
    S = sessionmaker(bind=engine)
    s = S()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pg_ready
class TestPgOnlyPaths:
    def test_import_return_curves_on_conflict_idempotent(self, pg_session):
        """postgresql.insert ... on_conflict_do_nothing 在真 PG 上幂等。"""
        from sqlalchemy import func, select
        from app.model import ReturnCurve
        from app.ingest.writer import import_return_curves
        s = pg_session
        recs = [{"country": "PGSMOKE", "risk_lvl": "R3", "year": 2099,
                 "rate": 7.77, "source_file": "pg-smoke.md"}]
        n1 = import_return_curves(s, [dict(r) for r in recs])
        n2 = import_return_curves(s, [dict(r) for r in recs])
        total = s.execute(select(func.count()).select_from(ReturnCurve).where(
            ReturnCurve.country == "PGSMOKE")).scalar()
        try:
            assert n1["n"] == 1 and n2["n"] == 0   # 第二轮被 ON CONFLICT 跳过
            assert total == 1
        finally:
            s.query(ReturnCurve).filter(ReturnCurve.country == "PGSMOKE").delete()
            s.commit()

    def test_search_retrieve_cosine_runs(self, pg_session):
        """search.retrieve 的 cosine_distance 查询在真 PG 上可编译执行（4096 维零向量）。"""
        from sqlalchemy import text as _t
        from app.search.search import retrieve
        # search_index.embedding 维度=4096；空表/有表均应返回 list 而非报错
        hits = retrieve(pg_session, [0.0] * 4096, k=3)
        assert isinstance(hits, list)
        _ = pg_session.execute(_t("SELECT 1")).scalar() == 1
