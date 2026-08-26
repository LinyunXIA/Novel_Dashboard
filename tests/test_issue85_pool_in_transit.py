"""Unit tests for issue #85：专款池在途 scope（DESIGN §19.4）。

净值口径 = 银行 + 专款池合计；投资期间（划出后~赎回前）总净值**不变**，资金在途不凹陷。

覆盖：
1. 日级 as-of（calendar.snapshot_as_of）：投资期间（6-30/11-30）family:total 与不投资一致；
2. 赎回后 family 只增收益（含负收益）；
3. 年快照（rebuild_snapshots）：未赎回年末 family:total/entity:* 同样加回在途本金；
4. pool_in_transit 直接单测。
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import BigInteger, Column, Integer, create_engine
from sqlalchemy.orm import sessionmaker

from app.core.calendar import snapshot_as_of
from app.core.invest import create_investment, redeem_investment
from app.core.snapshot import pool_in_transit, rebuild_snapshots
from app.db import Base
from app.model import Account, Entity, ExchangeRate, LedgerEntry, ReturnCurve, Snapshot


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


def _seed(session, *, cash: float = 1000, cur: str = "BEF", fx_to_usd: float = 0.02):
    """Henri Peeters BEF 账户 + 1980 现金 + BEF→USD 汇率 + 1980 比利时 R3 收益 10%。"""
    h = Entity(entity_type="person", name="Henri Peeters")
    session.add(h)
    session.flush()
    a = Account(entity_id=h.id, currency=cur)
    session.add(a)
    session.flush()
    session.add(LedgerEntry(account_id=a.id, date=date(1980, 1, 1),
                            inflow=cash, balance=cash, kind="income", reason="初始现金"))
    session.add(ExchangeRate(fx_from=cur, fx_to="USD", year=1980, rate=fx_to_usd))
    session.add(ReturnCurve(country="欧洲", risk_lvl="R3", year=1980, rate=10.0))
    session.flush()
    return h, a


def _family_asof(session, as_of: date) -> float:
    snaps = snapshot_as_of(session, as_of)
    return next(s["value"] for s in snaps if s["scope"] == "family:total")


def _family(session, y: int):
    """从 Snapshot 表读该年 family:total（rebuild 结果）。"""
    for s in session.query(Snapshot).filter(
            Snapshot.scope == "family:total", Snapshot.as_of_year == y):
        return float(s.value)
    return None


def test_mid_year_asof_invariant_during_invest( session):
    """5-01 投 100、未赎回：6-30 / 11-30 family:total 与不投资一致（1000 BEF = 20 USD）。"""
    h, a = _seed(session)
    base = _family_asof(session, date(1980, 6, 30))
    assert base == pytest.approx(20.0)                      # 1000 BEF × 0.02

    create_investment(session, year=1980, region="欧洲", risk_lvl="R3",
                      start_date=date(1980, 5, 1),
                      allocs=[{"entity_id": h.id, "currency": "BEF", "amount": 100}])
    session.flush()

    assert _family_asof(session, date(1980, 6, 30)) == pytest.approx(base)   # 不变
    assert _family_asof(session, date(1980, 11, 30)) == pytest.approx(base)  # 不变
    # entity 口径同样不变（银行 900 + 在途 100 = 1000）
    e = next(s["value"] for s in snapshot_as_of(session, date(1980, 6, 30))
             if s["scope"] == f"entity:{h.id}:BEF")
    assert e == pytest.approx(1000.0)
    # account scope 仍是纯银行（划出凹陷），在途不并入账户现金
    acc = next(s["value"] for s in snapshot_as_of(session, date(1980, 6, 30))
               if s["scope"].startswith("account:"))
    assert acc == pytest.approx(900.0)


def test_redeem_sets_pool_zero_and_returns_only_increment( session):
    """赎回后池清空；下一年度 family 只反映收益增量。"""
    h, a = _seed(session)
    inv = create_investment(session, year=1980, region="欧洲", risk_lvl="R3",
                            start_date=date(1980, 5, 1),
                            allocs=[{"entity_id": h.id, "currency": "BEF", "amount": 100}])
    session.flush()
    assert pool_in_transit(session, date(1980, 6, 30)) == {(h.id, "BEF"): pytest.approx(100)}

    redeem_investment(session, inv)
    session.flush()
    # 赎回日 12-30 起 pool 归零
    assert pool_in_transit(session, date(1980, 12, 30)) == {}
    # 12-31 family = 本金 1000 − 100 + 赎回 100 + 收益 → 只增收益
    interest = _interest(session, inv)
    assert _family_asof(session, date(1980, 12, 31)) == pytest.approx(
        round((1000 + interest) * 0.02, 2))


def test_annual_snapshot_includes_unredeemed_pool( session):
    """年末未赎回：rebuild 的 family:total/entity:* 加回在途本金（净值=银行+池）。"""
    h, a = _seed(session)
    base = _family_asof(session, date(1980, 12, 30))        # 未投资基准：1000 BEF = 20 USD
    create_investment(session, year=1980, region="欧洲", risk_lvl="R3",
                      start_date=date(1980, 5, 1),
                      allocs=[{"entity_id": h.id, "currency": "BEF", "amount": 100}])
    session.flush()
    rebuild_snapshots(session, range(1980, 1981))
    # 银行 900 + 在途 100 = 1000 BEF = 20 USD，与不投资基准一致
    assert _family(session, 1980) == pytest.approx(20.0)
    assert _family(session, 1980) == pytest.approx(base)
    ent = next(float(s.value) for s in session.query(Snapshot).filter(
        Snapshot.scope == f"entity:{h.id}:BEF", Snapshot.as_of_year == 1980))
    assert ent == pytest.approx(1000.0)


def test_annual_snapshot_redeemed_family( session):
    """年末已赎回：rebuild 的 family:total 只反映收益增量（池清空）。"""
    h, a = _seed(session)
    inv = create_investment(session, year=1980, region="欧洲", risk_lvl="R3",
                            start_date=date(1980, 5, 1),
                            allocs=[{"entity_id": h.id, "currency": "BEF", "amount": 100}])
    redeem_investment(session, inv)
    session.flush()
    rebuild_snapshots(session, range(1980, 1981))
    interest = _interest(session, inv)
    assert _family(session, 1980) == pytest.approx(round((1000 + interest) * 0.02, 2))


def _interest(session, inv) -> float:
    from app.core.invest import compute_interest
    return compute_interest(session, inv)[0]["interest"]


def test_pool_in_transit_edge( session):
    """start_date 当日入池；赎回前在途；跨期无输入返回空。"""
    h, a = _seed(session)
    assert pool_in_transit(session, date(1980, 4, 30)) == {}   # 投资前无池
    inv = create_investment(session, year=1980, region="欧洲", risk_lvl="R3",
                            start_date=date(1980, 5, 1),
                            allocs=[{"entity_id": h.id, "currency": "BEF", "amount": 50}])
    session.flush()
    assert pool_in_transit(session, date(1980, 5, 1)) == {(h.id, "BEF"): pytest.approx(50)}
    assert pool_in_transit(session, date(1980, 12, 29)) == {(h.id, "BEF"): pytest.approx(50)}
    redeem_investment(session, inv)
    session.flush()
    assert pool_in_transit(session, date(1980, 12, 30)) == {}