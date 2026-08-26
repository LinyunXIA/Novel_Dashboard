"""issue #116 回归：权威汇率表变更 → upsert 更新；非权威 insert-only 不变。"""
from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, create_engine, Integer, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.ingest.writer import import_fx
from app.model import ExchangeRate


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


def _rec(f, t, year, rate):
    return {"fx_from": f, "fx_to": t, "year": year, "rate": rate}


def test_insert_only_default_never_touches_existing(session):
    session.add(ExchangeRate(fx_from="USD", fx_to="SEK", year=1990, rate="5.00"))
    session.commit()
    st = import_fx(session, [_rec("USD", "SEK", 1990, "9.99"),
                             _rec("USD", "SEK", 1991, "6.00")])
    assert st == {"n": 1, "updated": 0}
    r = session.execute(select(ExchangeRate).where(
        ExchangeRate.fx_from == "USD", ExchangeRate.fx_to == "SEK",
        ExchangeRate.year == 1990)).scalar_one()
    from decimal import Decimal
    assert Decimal(str(r.rate)) == Decimal("5.00")


def test_authority_update_overwrites_changed_rates(session):
    session.add(ExchangeRate(fx_from="USD", fx_to="SEK", year=1990, rate="5.00"))
    session.add(ExchangeRate(fx_from="USD", fx_to="SEK", year=1992, rate="6.00"))
    session.commit()
    st = import_fx(session, [
        _rec("USD", "SEK", 1990, "5.50"),   # 值变 → 更新
        _rec("USD", "SEK", 1991, "5.20"),   # 新增
        _rec("USD", "SEK", 1992, "6.00"),   # 同值 → 不计更新
    ], update=True)
    assert st == {"n": 1, "updated": 1}
    rates = {r.year: str(r.rate) for r in session.execute(
        select(ExchangeRate).where(ExchangeRate.fx_from == "USD",
                                   ExchangeRate.fx_to == "SEK")).scalars()}
    from decimal import Decimal
    assert {y: Decimal(str(v)) for y, v in rates.items()} == {
        1990: Decimal("5.50"), 1991: Decimal("5.20"), 1992: Decimal("6.00")}


def test_year_null_constant_updated_separately(session):
    session.add(ExchangeRate(fx_from="EUR", fx_to="SEK", year=None, rate="9.00"))
    session.commit()
    st = import_fx(session, [_rec("EUR", "SEK", None, "9.24")], update=True)
    assert st == {"n": 0, "updated": 1}
