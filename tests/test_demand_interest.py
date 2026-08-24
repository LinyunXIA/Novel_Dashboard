"""Unit tests for app/core/demand.py（§19.2 活期 2% 年化按日折 · 审计修复补齐）。

覆盖：逐段余额加权计息公式（独立 oracle）、同年重跑幂等整笔覆盖、
关池/零余额/负余额账户跳过、未来年份 422、finance_entry 镜像 + 编年史 overlay 标签。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import BigInteger, Column, Integer, create_engine
from sqlalchemy.orm import sessionmaker

from app.core.demand import accrue_demand_interest, quote_demand_interest
from app.core.invest import ValidationError
from app.db import Base
from app.model import (Account, Entity, FinanceEntry, LedgerEntry, TimelineEvent)


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


_NAME_SEQ = {"n": 0}


def _acct(session, *, cur: str = "BEF", status: str = "active",
          closed_on=None) -> tuple[Entity, Account]:
    _NAME_SEQ["n"] += 1                       # UNIQUE(entity_type, name)：每次唯一名
    h = Entity(entity_type="person", name=f"Henri Peeters #{_NAME_SEQ['n']}")
    session.add(h)
    session.flush()
    a = Account(entity_id=h.id, currency=cur, status=status, closed_on=closed_on)
    session.add(a)
    session.flush()
    return h, a


def _entry(session, a: Account, d: date, inflow=None, outflow=None):
    session.add(LedgerEntry(account_id=a.id, date=d, inflow=inflow,
                            outflow=outflow, kind="income" if inflow else "expense"))
    session.flush()


def test_accrual_daily_weighted_formula(session):
    """独立 oracle：1000 常驻至 03-01 划出 200 → 加权 = 1000×d1 + 800×d2。"""
    _, a = _acct(session)
    _entry(session, a, date(1974, 1, 1), inflow=1000)
    _entry(session, a, date(1980, 3, 1), outflow=200)
    settle = date(1980, 12, 30)
    expected = (Decimal(1000) * (date(1980, 3, 1) - date(1980, 1, 1)).days
                + Decimal(800) * (settle - date(1980, 3, 1)).days)
    expected_interest = (expected * Decimal("0.02") / Decimal(365)).quantize(Decimal("0.01"))

    out = accrue_demand_interest(session, 1980)
    assert out["accounts"] == 1
    row = session.query(LedgerEntry).filter(
        LedgerEntry.note.like("%demand#1980%")).one()
    assert row.date == settle and row.kind == "income"
    assert Decimal(row.inflow) == expected_interest


def test_accrual_idempotent_rerun_overwrites(session):
    """同年重跑：先整笔抹除旧写入再重记，不双计。"""
    _, a = _acct(session)
    _entry(session, a, date(1990, 1, 1), inflow=10000)
    first = accrue_demand_interest(session, 1990)
    second = accrue_demand_interest(session, 1990)
    rows = session.query(LedgerEntry).filter(
        LedgerEntry.note.like("%demand#1990%")).all()
    fins = session.query(FinanceEntry).filter(
        FinanceEntry.label.like("%demand#1990%")).all()
    tls = session.query(TimelineEvent).filter(
        TimelineEvent.note.like("%demand#1990%"),
        TimelineEvent.overlay.is_(True)).all()
    assert len(rows) == len(fins) == len(tls) == 1
    assert float(rows[0].inflow) == pytest.approx(first["total_by_currency"]["BEF"])
    assert second["total_by_currency"] == first["total_by_currency"]


def test_accrual_skips_closed_and_zero_accounts(session):
    _, closed = _acct(session, status="closed", closed_on=date(2002, 1, 1))
    _entry(session, closed, date(2001, 1, 1), inflow=5000)
    _, empty = _acct(session)                       # 窗口内零余额、无流水
    out = accrue_demand_interest(session, 2001)
    assert quote_demand_interest(session, 2001) == []
    assert out["accounts"] == 0 and out["total_by_currency"] == {}


def test_accrual_negative_period_skipped(session):
    """全年净负债 → 不罚息、不生行。"""
    _, a = _acct(session)
    _entry(session, a, date(1995, 1, 1), outflow=300)   # 全年余额 -300
    out = accrue_demand_interest(session, 1995)
    assert out["accounts"] == 0


def test_accrual_future_year_rejected(session):
    _, a = _acct(session)
    _entry(session, a, date.today() - timedelta(days=365), inflow=1000)
    with pytest.raises(ValidationError) as e:
        accrue_demand_interest(session, date.today().year + 2)
    assert e.value.status == 422
    assert "结算日" in e.value.detail


def test_accrual_writes_finance_mirror_and_timeline_tag(session):
    h, a = _acct(session)
    _entry(session, a, date(2005, 1, 1), inflow=100000)
    accrue_demand_interest(session, 2005)
    fin = session.query(FinanceEntry).filter(
        FinanceEntry.label.like("%demand#2005%")).one()
    assert fin.entity_id == h.id and fin.kind == "income" and fin.source == "ui"
    tl = session.query(TimelineEvent).filter(
        TimelineEvent.note.like("%demand#2005%"),
        TimelineEvent.overlay.is_(True)).one()
    assert tl.event_year == 2005 and tl.event_date == date(2005, 12, 30)


def test_accrual_mid_year_inflow_counts_only_after_arrival(session):
    """年中才入账的资金只按在账天数计息。"""
    _, a = _acct(session)
    _entry(session, a, date(2010, 7, 1), inflow=365000)   # 恰好每天 1000 × 182 天
    quotes = quote_demand_interest(session, 2010)
    days = (date(2010, 12, 30) - date(2010, 7, 1)).days
    expected = (Decimal(365000) * days * Decimal("0.02") / Decimal(365)).quantize(Decimal("0.01"))
    assert len(quotes) == 1
    assert quotes[0]["interest"] == expected
