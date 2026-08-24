"""Unit tests for app/core/transfer.py（F-P1-03 · DESIGN §19.5）。

覆盖：同币划拨两笔净 0、跨币缺该年汇率 → 422、换汇折算正确、转出向后全链破负拒绝。
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import BigInteger, Integer, create_engine
from sqlalchemy.orm import sessionmaker

from app.core.invest import ValidationError
from app.core.transfer import transfer
from app.db import Base
from app.model import Account, Entity, ExchangeRate, LedgerEntry


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


def _seed(session):
    h = Entity(entity_type="person", name="Henri Peeters")
    j = Entity(entity_type="company", name="Peeters BV")
    session.add_all([h, j])
    session.flush()
    a1 = Account(entity_id=h.id, currency="BEF")   # Henri BEF
    a2 = Account(entity_id=j.id, currency="BEF")   # 公司 BEF（同币划拨目标）
    a3 = Account(entity_id=h.id, currency="EUR")   # Henri EUR（换汇目标）
    session.add_all([a1, a2, a3])
    session.flush()
    session.add(LedgerEntry(account_id=a1.id, date=date(1974, 1, 1),
                            inflow=1000, balance=1000, kind="income", reason="初始现金"))
    session.flush()
    return h, j, a1, a2, a3


def test_same_currency_transfer_net_zero(session):
    h, j, a1, a2, a3 = _seed(session)
    out = transfer(session, source_account_id=a1.id, target_entity_id=j.id,
                   target_currency="BEF", amount=300, year=1980)
    session.flush()
    assert out["operation"] == "划拨"
    rows = session.query(LedgerEntry).filter(
        LedgerEntry.reason.like("%划拨%") | LedgerEntry.reason.like("%收到划拨%")).all()
    # 源出 300 + 目标入 300
    outflow = sum(float(r.outflow or 0) for r in rows)
    inflow = sum(float(r.inflow or 0) for r in rows)
    assert outflow == pytest.approx(300)
    assert inflow == pytest.approx(300)
    assert outflow == inflow  # 净额 0


def test_fx_missing_rate_rejected(session):
    h, j, a1, a2, a3 = _seed(session)
    # 无 BEF→EUR 1980 汇率行
    with pytest.raises(ValidationError):
        transfer(session, source_account_id=a1.id, target_entity_id=h.id,
                 target_currency="EUR", amount=100, year=1980)


def test_fx_conversion_correct(session):
    h, j, a1, a2, a3 = _seed(session)
    session.add(ExchangeRate(fx_from="BEF", fx_to="EUR", year=1980, rate=0.024789))
    session.flush()
    out = transfer(session, source_account_id=a1.id, target_entity_id=h.id,
                   target_currency="EUR", amount=100, year=1980)
    session.flush()
    assert out["operation"] == "换汇"
    assert out["target_amount"] == pytest.approx(round(100 * 0.024789, 2), abs=0.01)


def test_transfer_causing_negative_rejected(session):
    h, j, a1, a2, a3 = _seed(session)
    # 该账户 1981 又有一笔 950 支出 → 1980 划出 60 后 1981 年未拐负
    session.add(LedgerEntry(account_id=a1.id, date=date(1981, 6, 30),
                            outflow=950, kind="expense", reason="大额支出"))
    session.flush()
    with pytest.raises(ValidationError):
        transfer(session, source_account_id=a1.id, target_entity_id=j.id,
                 target_currency="BEF", amount=60, year=1980)