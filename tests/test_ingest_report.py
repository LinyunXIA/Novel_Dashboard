"""issue #118 回归：冲突/解析失败报告持久化到 ingest_report 表。

- _record_findings：problems→block、warnings→warn，含 rule/line/detail；
- _record_parse_error：level='error'；
- level CHECK 约束拒绝非法值。
"""
from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, create_engine, Integer, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.exc import IntegrityError

from app.db import Base
from app.ingest.conflict import ConflictReport
from app.ingest.main import _record_findings, _record_parse_error
from app.model import IngestReport


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


def _crep() -> ConflictReport:
    c = ConflictReport(file="经济/银行/祖父.md")
    c.add("H4", 12, "既有末余额 100 ≠ 新首笔 200")
    c.warnings.append({"rule": "H5", "line": 3, "detail": "引用未知实体"})
    return c


def test_findings_persisted_with_levels(session):
    _record_findings(session, "经济/银行/祖父.md", _crep())
    session.commit()
    rows = session.execute(select(IngestReport).order_by(IngestReport.level)).scalars().all()
    assert [(r.level, r.rule, r.line) for r in rows] == [
        ("block", "H4", 12), ("warn", "H5", 3)]
    assert "新旧值" in rows[0].detail or "100" in rows[0].detail


def test_parse_error_persisted(session):
    _record_parse_error(session, "基准/汇率/新表.md", "fx", "ValueError: 表头不识别")
    session.commit()
    row = session.execute(select(IngestReport)).scalar_one()
    assert row.level == "error"
    assert row.rule is None
    assert "表头不识别" in row.detail


def test_level_check_constraint(session):
    session.add(IngestReport(file_path="x.md", level="fatal", detail="d"))
    with pytest.raises(IntegrityError):
        session.commit()
