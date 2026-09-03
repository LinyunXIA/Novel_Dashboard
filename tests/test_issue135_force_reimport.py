"""issue #135 回归：--force / force_files 清场后必须重导。

合并事故（aaa50bf）：bypass 分支 purge 后 continue 吞掉了冲突检测+导入，
--force(#114 重浇灌) 与 F-P2-06「采纳新版本」均退化为「只删不补」。
本文件断言：
1. --force 对收益文件（issue #211 起为 basic_income 基本收入.md）：purge 后行数恢复（不归零）；
2. --force 对 salary：issue #220 起为按人替换式重导（旧行清除、新行落库，不清零不双份）；
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

BI_FILE = "基准/收益表/基本收入.md"   # issue #211：四类旧收益文件整合为 basic_income
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
    """basic_income 逐年记录（issue #211 起收益流统一形态）。"""
    recs = [{"holder": "养祖父", "stream_type": "security", "group_key": "祖产债券",
             "label": "祖产股票债券 · 债券收益", "currency": "BEF", "year": 1980,
             "amount": 40.0, "source_line": 12, "source_file": BI_FILE}]
    return IngestReport(results=[
        ParseResult(file=BI_FILE, category="basic_income", records=recs)])


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
    _mk_src(tmp_path, BI_FILE)
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
    _mk_src(tmp_path, BI_FILE)
    monkeypatch.setattr("app.ingest.main.run_ingest", lambda d: _sec_report())
    import_all(session, tmp_path, log=lambda m: None)
    n1 = _count(session, IncomeStream)
    session.commit()

    import_all(session, tmp_path, log=lambda m: None, force_files={BI_FILE})
    assert _count(session, IncomeStream) == n1 > 0


def test_force_replaces_salary_rows(session, monkeypatch, tmp_path):
    """issue #220：salary 改按人替换式后 --force 可安全重导——旧行清除、新行落库。

    （#114 原约束「--force 不支持薪资文件」随替换式退役：writer 先删该 entity
    旧 salary 流+镜像再插，不触碰 ledger，文件名更替/口径修正均安全。）
    """
    _mk_src(tmp_path, SAL_FILE)
    monkeypatch.setattr("app.ingest.main.run_ingest", lambda d: _sal_report())
    import_all(session, tmp_path, log=lambda m: None)
    assert _count(session, IncomeStream) == 1
    session.commit()

    logs: list[str] = []
    import_all(session, tmp_path, log=logs.append, force=True)
    assert _count(session, IncomeStream) == 1          # 替换式：删 1 插 1，不清零不双份
    assert _count(session, FinanceEntry) == 1
    assert any("替换式" in m for m in logs)
