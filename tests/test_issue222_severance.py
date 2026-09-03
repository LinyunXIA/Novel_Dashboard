"""issue #222：薪资文件表外「退职金专项核算」段导入。

退休年一次性税后退职金（比利时 Assigned out：2 倍基薪、EUR、18% 优惠税率）
并入 salary 流，group_key/label 用「退职金」与逐年薪资区分。
"""
from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, Integer, create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.ingest import writer
from app.ingest.parsers import parse_salary
from app.model import FinanceEntry, IncomeStream

SAL_DOC = (
    "| 年份 | 年度税前总收入 | 结算币种 | 综合税率 | 年度税后总收入 |\n"
    "| ---- | -------------- | -------- | -------- | -------------- |\n"
    "| 2011 | 4,902,405      | CNY      | 20%      | 3,921,924      |\n"
    "| 2012 | 5,098,502      | CNY      | 20%      | 4,078,801      |\n"
    "\n"
    "## 父亲2012年比利时退职金专项核算（Assigned out员工）\n"
    "6. 税前退职金：446,845 × 2 = **893,690 EUR**\n"
    "8. 税后退职金：893,690 × (1-18%) = **732,826 EUR**\n"
)

SAL_DOC_PLAIN = (
    "| 年份 | 年度税后总收入 | 币种 |\n"
    "| ---- | -------------- | ---- |\n"
    "| 2012 | 5,646,998      | CNY  |\n"
    "\n"
    "## 四、2012比利时退职金专项核算（Assigned out员工）\n"
    "- 税后退职金：747,584 EUR\n"
)


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


def test_parse_severance_bold(tmp_path):
    """bold 段取税后退职金（不取同行税前 893,690），年份取段标题 2012。"""
    p = tmp_path / "养父的薪资_CNY修正版.md"
    p.write_text(SAL_DOC, encoding="utf-8")
    recs, warns = parse_salary(p)
    assert warns == []
    sev = [r for r in recs if r.get("component") == "severance"]
    assert len(sev) == 1
    assert sev[0] == {"holder": "养父", "year": 2012, "currency": "EUR",
                      "after_tax": 732826.0, "component": "severance"}
    # 逐年薪资 2 行不受影响
    salary = [r for r in recs if r.get("component") != "severance"]
    assert len(salary) == 2 and salary[1]["currency"] == "CNY"


def test_parse_severance_plain_fallback(tmp_path):
    """无 bold 时兜底取行末「数字 币种」（养母形态：- 税后退职金：747,584 EUR）。"""
    p = tmp_path / "养母的薪资_CNY修正版.md"
    p.write_text(SAL_DOC_PLAIN, encoding="utf-8")
    recs, warns = parse_salary(p)
    assert warns == []
    sev = [r for r in recs if r.get("component") == "severance"]
    assert len(sev) == 1
    assert sev[0]["after_tax"] == 747584.0 and sev[0]["currency"] == "EUR"
    assert sev[0]["year"] == 2012 and sev[0]["holder"] == "养母"


def test_writer_severance_labels_and_replace(session):
    """退职金落 salary 流、label/group_key 区分；替换式重导不双份。"""
    recs = [
        {"holder": "养父", "year": 2012, "after_tax": 4078801.0, "currency": "CNY",
         "source_file": "养父的薪资_CNY修正版.md"},
        {"holder": "养父", "year": 2012, "after_tax": 732826.0, "currency": "EUR",
         "component": "severance", "source_file": "养父的薪资_CNY修正版.md"},
    ]
    st = writer.import_salary(session, recs)
    session.flush()
    assert st["stream"] == 2
    streams = session.execute(select(IncomeStream)).scalars().all()
    by_cur = {r.currency: r for r in streams}
    assert by_cur["EUR"].group_key == "Joren Peeters退职金"
    assert by_cur["EUR"].label == "Joren Peeters退职金税后"
    assert by_cur["CNY"].group_key == "Joren Peeters薪资"
    fe = session.execute(select(FinanceEntry).where(
        FinanceEntry.entity_id == by_cur["EUR"].entity_id)).scalars().all()
    assert {f.label for f in fe} == {"Joren Peeters薪资税后", "Joren Peeters退职金税后"}

    # 二跑（替换式）：仍 2 行，镜像不双份
    writer.import_salary(session, recs)
    session.flush()
    n_stream = len(session.execute(select(IncomeStream)).scalars().all())
    n_fe = len(session.execute(select(FinanceEntry)).scalars().all())
    assert n_stream == 2 and n_fe == 2

    # 口径变更（退职金金额修订）重导：旧退职金行被替换，无残留
    recs2 = [dict(r) for r in recs]
    recs2[1]["after_tax"] = 700000.0
    writer.import_salary(session, recs2)
    session.flush()
    eur = session.execute(select(IncomeStream).where(
        IncomeStream.currency == "EUR")).scalars().all()
    assert len(eur) == 1 and eur[0].amount == 700000.0
    fe_eur = session.execute(select(FinanceEntry).where(
        FinanceEntry.currency == "EUR")).scalars().all()
    assert len(fe_eur) == 1 and fe_eur[0].amount == 700000.0
