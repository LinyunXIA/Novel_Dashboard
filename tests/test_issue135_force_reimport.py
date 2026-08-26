"""issue #135 回归：--force / force_files 清场后必须重导。

合并事故（aaa50bf）：bypass 分支 purge 后 continue 吞掉了冲突检测+导入，
--force(#114 重浇灌) 与 F-P2-06「采纳新版本」均退化为「只删不补」。
本文件断言：
1. --force 对四类收益文件：purge 后行数恢复（不归零）；
2. --force 对 salary：整文件跳过、不清场（#114 原约束保留）；
3. force_files（版本采纳）：同样必须重导恢复。
"""
from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, Integer, create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.ingest.main import import_all
from app.ingest.parse import IngestReport, ParseResult
from app.model import FinanceEntry, IncomeStream

SEC_FILE = "基准/收益表/祖产股票债券收益.md"
SAL_FILE = "基准/薪资/养父薪资.md"


@pytest.fixture
def session():
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


def _sec_report() -> IngestReport:
    recs = [{"holder": "养祖父", "name": "测试券", "face_value": 1000.0,
             "currency": "BEF", "rate_pct": 4.0, "source_file": SEC_FILE}]
    return IngestReport(results=[
        ParseResult(file=SEC_FILE, category="income_security", records=recs)])


def _sal_report() -> IngestReport:
    recs = [{"holder": "养父", "year": 1990, "after_tax": 100.0,
             "currency": "BEF", "source_file": SAL_FILE}]
    return IngestReport(results=[
        ParseResult(file=SAL_FILE, category="salary", records=recs)])


def _count(session, model) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar()


def _mk_src(tmp_path, rel: str):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("stub\n", encoding="utf-8")


def test_force_reirrigates_after_purge(session, monkeypatch, tmp_path):
    _mk_src(tmp_path, SEC_FILE)
    monkeypatch.setattr("app.ingest.main.run_ingest", lambda d: _sec_report())
    import_all(session, tmp_path, log=lambda m: None)
    n1 = _count(session, IncomeStream)
    assert n1 > 0
    assert _count(session, FinanceEntry) == n1
    session.commit()

    # 回归点：--force 清场后必须重导回同等行数（修复前 purge+continue → 0）
    import_all(session, tmp_path, log=lambda m: None, force=True)
    n2 = _count(session, IncomeStream)
    assert n2 == n1 > 0
    assert _count(session, FinanceEntry) == n2


def test_force_files_adopt_reimports(session, monkeypatch, tmp_path):
    """F-P2-06「采纳新版本」走 force_files 路径，同样不得只删不补。"""
    _mk_src(tmp_path, SEC_FILE)
    monkeypatch.setattr("app.ingest.main.run_ingest", lambda d: _sec_report())
    import_all(session, tmp_path, log=lambda m: None)
    n1 = _count(session, IncomeStream)
    session.commit()

    import_all(session, tmp_path, log=lambda m: None, force_files={SEC_FILE})
    assert _count(session, IncomeStream) == n1 > 0


def test_force_skips_salary_without_purge(session, monkeypatch, tmp_path):
    """#114 原约束：--force 不支持薪资文件——不清场也不重导。"""
    _mk_src(tmp_path, SAL_FILE)
    monkeypatch.setattr("app.ingest.main.run_ingest", lambda d: _sal_report())
    import_all(session, tmp_path, log=lambda m: None)
    assert _count(session, IncomeStream) == 1
    session.commit()

    logs: list[str] = []
    import_all(session, tmp_path, log=logs.append, force=True)
    assert _count(session, IncomeStream) == 1          # 未被 purge（修复前归零）
    assert any("--force 不支持薪资文件" in m for m in logs)
