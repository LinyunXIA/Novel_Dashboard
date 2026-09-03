"""issue #220：salary 改按人替换式落库。

薪资文件是某人入职至退休的逐年全量台账（salary 流唯一写入方）。文件名更替
（养父的薪资.md → 养父的薪资_CNY修正版.md）或口径修正（中国段 USD→CNY）时，
导入须**整段替换**该 entity 的旧 salary income_stream + finance_entry 镜像，
不能同名插入导致同年 BEF/USD/CNY 多份薪资。
"""
from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, Integer, create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.ingest import writer
from app.model import Entity, FinanceEntry, IncomeStream

OLD_FILE = "基准/薪资/养父的薪资.md"
NEW_FILE = "基准/薪资/养父的薪资_CNY修正版.md"
MOTHER_FILE = "基准/薪资/养母的薪资_CNY修正版.md"


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


def _rows(s):
    return s.execute(select(IncomeStream).order_by(IncomeStream.year)).scalars().all()


def test_replace_clears_old_file_rows(session):
    """老文件行（含错误币种 BEF/USD）在新文件导入后被整段清除替换。"""
    # 老台账：1990 BEF（比利时段）+ 2011 USD（中国段，老口径）
    writer.import_salary(session, [
        {"holder": "养父", "year": 1990, "after_tax": 422890.0,
         "currency": "BEF", "source_file": OLD_FILE},
        {"holder": "养父", "year": 2011, "after_tax": 459471.0,
         "currency": "USD", "source_file": OLD_FILE},
    ])
    session.flush()
    assert len(_rows(session)) == 2

    # CNY 修正版：1990 BEF（舍入微差）+ 2011 CNY（口径修正）
    st = writer.import_salary(session, [
        {"holder": "养父", "year": 1990, "after_tax": 422890.0,
         "currency": "BEF", "source_file": NEW_FILE},
        {"holder": "养父", "year": 2011, "after_tax": 3921924.0,
         "currency": "CNY", "source_file": NEW_FILE},
    ])
    session.flush()

    assert st["replaced"] == 2 and st["stream"] == 2
    rows = _rows(session)
    assert len(rows) == 2                          # 不是 4——无同年双份
    by_year = {r.year: r for r in rows}
    assert by_year[2011].currency == "CNY"
    assert by_year[2011].amount == 3921924.0
    assert by_year[2011].source_file == NEW_FILE
    assert all(r.source_file == NEW_FILE for r in rows)
    # 镜像同步替换：2 行、无 USD 残留
    fe = session.execute(select(FinanceEntry).where(
        FinanceEntry.label == "Joren Peeters薪资税后")).scalars().all()
    assert len(fe) == 2
    assert {f.currency for f in fe} == {"BEF", "CNY"}
    assert sum(1 for f in fe if f.source_file == OLD_FILE) == 0


def test_replace_idempotent(session):
    """二跑：行数恒定（替换式幂等）。"""
    recs = [
        {"holder": "养父", "year": 1989, "after_tax": 88028.0,
         "currency": "USD", "source_file": NEW_FILE},
        {"holder": "养父", "year": 1998, "after_tax": 1840162.0,
         "currency": "CNY", "source_file": NEW_FILE},
    ]
    writer.import_salary(session, recs)
    writer.import_salary(session, recs)
    session.flush()
    n_stream = session.execute(
        select(func.count()).select_from(IncomeStream)).scalar()
    n_fe = session.execute(
        select(func.count()).select_from(FinanceEntry)).scalar()
    assert n_stream == 2 and n_fe == 2


def test_replace_scoped_per_person(session):
    """替换按 entity 隔离：养父重导不影响养母既有薪资行。"""
    writer.import_salary(session, [
        {"holder": "养父", "year": 2011, "after_tax": 459471.0,
         "currency": "USD", "source_file": OLD_FILE},
        {"holder": "养母", "year": 2011, "after_tax": 662892.0,
         "currency": "USD", "source_file": "基准/薪资/养母的薪资.md"},
    ])
    session.flush()
    writer.import_salary(session, [
        {"holder": "养父", "year": 2011, "after_tax": 3921924.0,
         "currency": "CNY", "source_file": NEW_FILE},
    ])
    session.flush()
    rows = _rows(session)
    assert len(rows) == 2                          # 养父 1（替换）+ 养母 1（保留）
    by_holder = {}
    for r in rows:
        ent = session.get(Entity, r.entity_id)
        by_holder[ent.name] = r
    assert by_holder["Joren Peeters"].currency == "CNY"
    assert by_holder["Johanna Peeters"].currency == "USD"
    assert by_holder["Johanna Peeters"].amount == 662892.0
    # 养母镜像原样保留
    fe_m = session.execute(select(FinanceEntry).where(
        FinanceEntry.label == "Johanna Peeters薪资税后")).scalars().all()
    assert len(fe_m) == 1 and fe_m[0].currency == "USD"
