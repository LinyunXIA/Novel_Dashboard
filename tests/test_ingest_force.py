"""issue #114 回归：--force 重导前的派生行清场（_purge_income_derived）。

只清该 source_file 名下的 income_stream + finance_entry 镜像，
不触碰其他文件/其他表的数据。
"""
from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, create_engine, func, select, Integer
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.ingest.main import _purge_income_derived
from app.model import Entity, FinanceEntry, IncomeStream


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


def test_purge_only_target_file_rows(session):
    e = Entity(entity_type="person", name="Henri")
    session.add(e)
    session.flush()
    session.add_all([
        IncomeStream(entity_id=e.id, stream_type="rent", group_key="g", currency="BEF",
                     year=1980, amount=1, label="l", source_file="基准/收益表/惠民租房.md"),
        IncomeStream(entity_id=e.id, stream_type="rent", group_key="g", currency="BEF",
                     year=1981, amount=1, label="l", source_file="基准/收益表/惠民租房.md"),
        FinanceEntry(entity_id=e.id, entity_kind="person", year=1980, kind="income",
                     amount=1, currency="BEF", label="l", source_file="基准/收益表/惠民租房.md"),
        # 其他文件的行必须保留
        IncomeStream(entity_id=e.id, stream_type="property", group_key="g2", currency="BEF",
                     year=1980, amount=2, label="l2", source_file="基准/收益表/经营性房产收益.md"),
    ])
    session.commit()

    n = _purge_income_derived(session, "基准/收益表/惠民租房.md")
    assert n == 3

    remain_is = session.execute(select(func.count()).select_from(IncomeStream)).scalar()
    remain_fe = session.execute(select(func.count()).select_from(FinanceEntry)).scalar()
    assert (remain_is, remain_fe) == (1, 0)
