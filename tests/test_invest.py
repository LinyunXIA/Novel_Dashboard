"""Unit tests for app/core/invest.py（F-P1-01/02 · DESIGN §19.1–19.4）。

覆盖：区域起始年下限 422、年度幂等 409（ConflictError.status）、as-of 超支 422、
覆盖连锁（向后全链破负）拒绝、compute_interest 公式、redeem 生成 pool + investment_income
两笔且余额连续。
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import BigInteger, Column, Integer, create_engine
from sqlalchemy.orm import sessionmaker

from app.core.invest import (
    ConflictError, ValidationError, compute_interest, create_investment, redeem_investment,
)
from app.core.recompute import recompute_all
from app.core.snapshot import account_balance_at
from app.db import Base
from app.model import Account, Entity, LedgerEntry, ReturnCurve


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


def _seed(session, *, cash: float = 1000, cur: str = "BEF"):
    """Henri Peeters BEF 账户 + 1974 年 1000 初始现金 + 1980 比利时 R3 收益 10%。"""
    h = Entity(entity_type="person", name="Henri Peeters")
    session.add(h)
    session.flush()
    a = Account(entity_id=h.id, currency=cur)
    session.add(a)
    session.flush()
    session.add(LedgerEntry(account_id=a.id, date=date(1974, 1, 1),
                            inflow=cash, balance=cash, kind="income", reason="初始现金"))
    session.add(ReturnCurve(country="比利时", risk_lvl="R3", year=1980, rate=10.0))
    session.flush()
    return h, a


def test_region_start_year_lower_bound(session):
    h, a = _seed(session)
    with pytest.raises(ValidationError):
        create_investment(session, year=1988, region="美国", risk_lvl="R3",
                          start_date=date(1988, 5, 1), allocs=[{"entity_id": h.id, "currency": "BEF", "amount": 100}])


def test_year_region_idempotency_conflict(session):
    h, a = _seed(session)
    kw = dict(year=1980, region="欧洲", risk_lvl="R3",
              allocs=[{"entity_id": h.id, "currency": "BEF", "amount": 100}])
    inv = create_investment(session, start_date=date(1980, 5, 1), **kw)
    session.flush()
    assert inv.id
    with pytest.raises(ConflictError) as e:
        create_investment(session, start_date=date(1980, 6, 1), **kw)
    assert e.value.status == 409


def test_asof_overdraw_422(session):
    h, a = _seed(session, cash=100)  # 仅 100 现金
    with pytest.raises(ValidationError):
        create_investment(session, year=1980, region="欧洲", risk_lvl="R3",
                          start_date=date(1980, 5, 1),
                          allocs=[{"entity_id": h.id, "currency": "BEF", "amount": 200}])  # >100


def test_coverage_chain_negative_rejected(session):
    h, a = _seed(session, cash=100)
    # 该账户 1981-12-31 又有一笔 100 支出（在 start_date 之后）→ 投入 50 后 12-31 拐负
    session.add(LedgerEntry(account_id=a.id, date=date(1981, 12, 31),
                            outflow=100, kind="expense", reason="后续支出"))
    session.flush()
    with pytest.raises(ValidationError):
        create_investment(session, year=1980, region="欧洲", risk_lvl="R3",
                          start_date=date(1981, 6, 1),
                          allocs=[{"entity_id": h.id, "currency": "BEF", "amount": 50}])


def test_batch_same_pool_accumulated_over_pool_rejected(session):
    """审计修复：批内同主体同币种多笔 alloc 按累计占用校验，两笔各自合法、合计超额须拦。"""
    h, a = _seed(session, cash=1000)
    allocs = [{"entity_id": h.id, "currency": "BEF", "amount": 600},
              {"entity_id": h.id, "currency": "BEF", "amount": 600}]  # 合计 1200 > 1000
    with pytest.raises(ValidationError) as e:
        create_investment(session, year=1980, region="欧洲", risk_lvl="R3",
                          start_date=date(1980, 5, 1), allocs=allocs)
    assert "批内累计" in e.value.detail


def test_batch_same_pool_cumulative_negative_simulation(session):
    """审计修复：破负模拟也按批内累计口径——单笔各自不破负、合计中途拐负须拦。

    1300 初始；1981-06-01 支出 500。各投 600：单独模拟均不破负（700−500=200），
    累计 1200 则 100−500 = −400 → 必须整体拒绝。
    """
    h, a = _seed(session, cash=1300)
    session.add(LedgerEntry(account_id=a.id, date=date(1981, 6, 1),
                            outflow=500, kind="expense", reason="年中支出"))
    session.flush()
    allocs = [{"entity_id": h.id, "currency": "BEF", "amount": 600},
              {"entity_id": h.id, "currency": "BEF", "amount": 600}]
    with pytest.raises(ValidationError):
        create_investment(session, year=1980, region="欧洲", risk_lvl="R3",
                          start_date=date(1980, 6, 1), allocs=allocs)


def test_batch_is_all_twice_second_rejected(session):
    """重复「全部」：第二笔剩余 ≤ 0 → 422（批内已占口径）。"""
    h, a = _seed(session, cash=800)
    allocs = [{"entity_id": h.id, "currency": "BEF", "is_all": True},
              {"entity_id": h.id, "currency": "BEF", "is_all": True}]
    with pytest.raises(ValidationError) as e:
        create_investment(session, year=1980, region="欧洲", risk_lvl="R3",
                          start_date=date(1980, 5, 1), allocs=allocs)
    assert "必须 > 0" in e.value.detail


def test_redeem_before_settlement_409(session):
    """审计修复：未到年末结算日（当年 12-30）不可赎回（409）。"""
    h, a = _seed(session, cash=1000)
    future = date.today().year + 10
    session.add(ReturnCurve(country="比利时", risk_lvl="R3", year=future, rate=10.0))
    session.flush()
    inv = create_investment(session, year=future, region="欧洲", risk_lvl="R3",
                            start_date=date(future, 6, 1),
                            allocs=[{"entity_id": h.id, "currency": "BEF", "amount": 100}])
    session.flush()
    with pytest.raises(ConflictError) as e:
        redeem_investment(session, inv)
    assert e.value.status == 409
    assert "结算日" in e.value.detail


def _create(session, *, start="1980-05-01", amount=100, is_all=False):
    h, a = _seed(session)
    inv = create_investment(session, year=1980, region="欧洲", risk_lvl="R3",
                            start_date=date.fromisoformat(start),
                            allocs=[{"entity_id": h.id, "currency": "BEF",
                                     "amount": amount, "is_all": is_all}])
    session.flush()
    return h, a, inv


def test_compute_interest_formula(session):
    h, a, inv = _create(session, start="1980-05-01", amount=100)
    days = (date(1980, 12, 30) - date(1980, 5, 1)).days
    got = compute_interest(session, inv)[0]
    assert got["days"] == days
    # R=10% → gross = 10/100/365*days；interest = 100*gross
    assert abs(got["interest"] - round(100 * (0.10 / 365) * days, 2)) < 0.01


def test_compute_interest_start_12_30_zero(session):
    h, a, inv = _create(session, start="1980-12-30", amount=100)
    got = compute_interest(session, inv)[0]
    assert got["days"] == 0
    assert got["interest"] == 0


def test_redeem_writes_two_entries_and_balances(session):
    h, a, inv = _create(session, amount=100, start="1980-05-01")
    recompute_all(session, 1974)
    session.flush()
    before = account_balance_at(session, a.id, date(1980, 12, 29))
    assert float(before) == pytest.approx(900)  # 1000 初始 − 100 划出

    out = redeem_investment(session, inv)
    session.flush()
    recompute_all(session, 1974)
    session.flush()
    assert out["allocs"] == 1

    kinds = {e.kind for e in session.query(LedgerEntry)
             .filter(LedgerEntry.kind.in_(["pool", "investment_income"])).all()}
    assert "pool" in kinds and "investment_income" in kinds

    # 赎回后余额 = 1000 − 100（投资）+ 100（本金）+ interest
    after = account_balance_at(session, a.id, date(1980, 12, 30))
    interest = compute_interest(session, inv)[0]["interest"]
    assert float(after) == pytest.approx(1000 + interest)


def test_redeem_double_409(session):
    h, a, inv = _create(session, amount=100, start="1980-05-01")
    redeem_investment(session, inv)
    session.flush()
    with pytest.raises(ConflictError):
        redeem_investment(session, inv)


def test_create_writes_investment_ledger_and_overlay(session):
    h, a, inv = _create(session, amount=100, start="1980-05-01")
    session.flush()
    invest_rows = session.query(LedgerEntry).filter(LedgerEntry.kind == "investment").all()
    assert len(invest_rows) == 1
    assert float(invest_rows[0].outflow) == pytest.approx(100)
    from app.model import TimelineEvent
    overlay = session.query(TimelineEvent).filter(TimelineEvent.overlay.is_(True)).all()
    assert len(overlay) == 1
    assert overlay[0].event_year == 1980