"""F-P2-02 块 A：持仓市值并入总资产/快照/health（§19.6 block A）。"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.calendar import snapshot_as_of
from app.core.health import check_holding_value, summarize
from app.core.snapshot import rebuild_snapshots
from app.core.stock_cost import apply_buy, apply_sell
from app.core.stock_wealth import market_value_at, portfolio_breakdown
from app.db import Base
from app.model import Account, Entity, HoldingEvent, LedgerEntry, Snapshot


@pytest.fixture
def session():
    from sqlalchemy import BigInteger, Integer
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


def _seed(session, *, cash=20000.0, shares=1000.0, unit_price=10.0):
    """USD 主体：2018 现金 20000 + 买入 10000（批次 shares=1000 @10）→ 净值口径 = cash+市值。"""
    e = Entity(entity_type="person", name="Stijn")
    session.add(e)
    session.flush()
    a = Account(entity_id=e.id, currency="USD")
    session.add(a)
    session.flush()
    session.add(LedgerEntry(account_id=a.id, date=date(2018, 1, 1), inflow=cash,
                            balance=cash, kind="income", reason="初次现金"))
    session.flush()
    apply_buy(session, entity_id=e.id, company="AAPL", ticker="AAPL", date=date(2018, 6, 1),
              unit_price=unit_price, shares=shares, event_id="w1", account_id=a.id)
    session.flush()
    return e, a


def _scope_value(session, scope: str, year: int) -> float | None:
    for s in session.execute(select(Snapshot).where(
            Snapshot.scope == scope, Snapshot.as_of_year == year)).scalars():
        return float(s.value)
    return None


def test_market_value_at_simple():
    """market_value_at 单测：买入后市值=shares×unit_price。"""
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, __import__("sqlalchemy").BigInteger) and col.primary_key:
                col.type = __import__("sqlalchemy").Integer()
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    e = Entity(entity_type="person", name="S"); s.add(e); s.flush()
    a = Account(entity_id=e.id, currency="USD"); s.add(a); s.flush()
    apply_buy(s, entity_id=e.id, company="AAPL", date=date(2018, 6, 1), unit_price=10.0,
              shares=1000.0, event_id="w1", account_id=a.id)
    assert market_value_at(s, e.id, date(2018, 12, 30)) == pytest.approx(10000.0)
    s.close(); eng.dispose()


def test_rebuild_snapshot_includes_holding_but_account_excludes(session):
    e, a = _seed(session)
    rebuild_snapshots(session, years=range(2017, 2019), from_year=2018)
    # entity / family 含市值：cash(20000-10000=10000) + 市值(10000) = 20000
    assert _scope_value(session, f"entity:{e.id}:USD", 2018) == pytest.approx(20000.0)
    assert _scope_value(session, "family:total", 2018) == pytest.approx(20000.0)
    # account scope 不含市值（只 10000）
    assert _scope_value(session, f"account:{a.id}:USD", 2018) == pytest.approx(10000.0)


def test_snapshot_asof_includes_holding(session):
    e, a = _seed(session)
    snaps = snapshot_as_of(session, date(2018, 12, 30))
    by = {s["scope"]: s["value"] for s in snaps}
    assert by[f"entity:{e.id}:USD"] == pytest.approx(20000.0)
    assert by["family:total"] == pytest.approx(20000.0)
    assert by[f"account:{a.id}:USD"] == pytest.approx(10000.0)


def test_health_holding_valuation_warn_and_summary_key(session):
    e = Entity(entity_type="person", name="Stijn")
    session.add(e)
    session.flush()
    session.add(HoldingEvent(entity_id=e.id, company="AAPL", date=date(2018, 1, 1),
                             event_type="buy", shares=100.0, unit_price=None,
                             source_file="t.md"))
    session.flush()
    finds = check_holding_value(session)
    assert any(f.rule == "H-STOCK" and f.level == "warn" for f in finds)
    assert "H-STOCK" in summarize(session)


def test_health_holding_ok_when_priced(session):
    e = Entity(entity_type="person", name="Stijn")
    session.add(e)
    session.flush()
    session.add(HoldingEvent(entity_id=e.id, company="AAPL", date=date(2018, 1, 1),
                             event_type="buy", shares=100.0, unit_price=5.0,
                             source_file="t.md"))
    session.flush()
    assert check_holding_value(session) == []


def test_rebuild_incremental_from_year_recomputes_holding(session):
    e, a = _seed(session)
    rebuild_snapshots(session, years=range(2017, 2019), from_year=2018)
    # 改动触发 from_year 增量重算：加一笔买入 → 净值净额守恒（现金-5000、市值+5000），entity 不变
    apply_buy(session, entity_id=e.id, company="MSFT", date=date(2018, 9, 1),
              unit_price=50.0, shares=100.0, event_id="w2", account_id=a.id)
    rebuild_snapshots(session, years=range(2017, 2019), from_year=2018)
    assert _scope_value(session, f"entity:{e.id}:USD", 2018) == pytest.approx(20000.0)
    assert market_value_at(session, e.id, date(2018, 12, 30)) == pytest.approx(15000.0)


# ---------- F-P2-03 follow-up：分拆/并购市值漏记修复（closed_on 结清窗口） ----------
def _seed_utx(session, entity_id=None, shares=48053700.0, unit_price=5.0):
    from app.core.stock_cost import _next_batch_ids
    if entity_id is None:
        e = Entity(entity_type="person", name="Stijn"); session.add(e); session.flush()
        entity_id = e.id
    b = next(_next_batch_ids(session, 1))
    session.add(HoldingEvent(entity_id=entity_id, company="UTX", date=date(2000, 12, 31),
                             event_type="buy", batch_id=b, shares=shares,
                             unit_price=unit_price, amount=shares * unit_price / 10000.0))
    session.flush()
    return entity_id


def _split_utx(session, eid):
    from app.core.stock_cost import apply_merger
    return apply_merger(session, {"entity_id": eid, "date": "2020-04-03", "old_company": "UTX",
                                  "form": "split",
                                  "legs": [{"company": "CARR", "per_old_share": 1.0},
                                           {"company": "OTIS", "per_old_share": 0.5},
                                           {"company": "RTX", "per_old_share": 1.0}]})


def test_split_preserves_pre_merger_value(session):
    utx_val = 48053700.0 * 5.0                      # 4805.37万股 × 5 成本 = 240M USD
    eid = _seed_utx(session)
    _split_utx(session, eid); session.flush()
    # 重构前年份：旧 UTX 仍计入（修复前恒为 0 → 漏记）
    assert market_value_at(session, eid, date(2010, 12, 30)) == pytest.approx(utx_val)
    # 重构后年份：新三家计入、UTX 已结清不重复计数；成本随链 → 总额仍 = utx_val
    assert market_value_at(session, eid, date(2021, 12, 30)) == pytest.approx(utx_val)
    assert portfolio_breakdown(session, date(2021, 12, 30))[eid] == pytest.approx(utx_val)


def test_merge_closed_hidden_from_open_batches(session):
    from app.core.stock_cost import _open_batches
    eid = _seed_utx(session)
    _split_utx(session, eid); session.flush()
    assert _open_batches(session, eid, "UTX") == []     # 结清后不再视为 open（FIFO/分红/后续并购）


def test_rebuild_split_years_include_upstream_value(session):
    e = Entity(entity_type="person", name="Stijn"); session.add(e); session.flush()
    a = Account(entity_id=e.id, currency="USD"); session.add(a); session.flush()
    session.add(LedgerEntry(account_id=a.id, date=date(2010, 1, 1), inflow=10000, balance=10000,
                            kind="income", reason="cash"))
    utx_val = 48053700.0 * 5.0
    _seed_utx(session, entity_id=e.id)
    _split_utx(session, e.id); session.flush()
    rebuild_snapshots(session, years=range(2009, 2022), from_year=2010)
    # 分拆前年（2015）：entity = 银行 10000 + UTX 市值（修复前漏记→只有 10000）
    assert _scope_value(session, f"entity:{e.id}:USD", 2015) == pytest.approx(10000.0 + utx_val)
    # 分拆后年（2021）：entity = 银行 10000 + 新三家市值（= 旧 UTX 市值，成本随链）
    assert _scope_value(session, f"entity:{e.id}:USD", 2021) == pytest.approx(10000.0 + utx_val)