"""F-P2-02 事件·股票：买入 / FIFO 卖出 / 分红 / 被动抬升引擎单测（§19.6 block B）。"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.stock_cost import (apply_buy, apply_dividend, apply_passive_uplift,
                                 apply_sell)
from app.core.stock_wealth import market_value_at
from app.db import Base
from app.model import Account, Entity, HoldingEvent, LedgerEntry


@pytest.fixture
def db():
    from sqlalchemy import BigInteger, Integer
    from sqlalchemy.pool import StaticPool
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    s = S()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _seed(db):
    e = Entity(entity_type="person", name="Stijn")
    db.add(e)
    db.flush()
    a = Account(entity_id=e.id, currency="USD")
    db.add(a)
    db.flush()
    return e.id, a.id


def _holding(db, entity_id, company):
    return db.execute(select(HoldingEvent).where(
        HoldingEvent.entity_id == entity_id,
        HoldingEvent.company == company).order_by(HoldingEvent.date, HoldingEvent.id)).scalars().all()


def _ledger_rows(db, account_id):
    return db.execute(select(LedgerEntry).where(
        LedgerEntry.account_id == account_id).order_by(LedgerEntry.id)).scalars().all()


# ---------- buy ----------
def test_buy_writes_batch_and_ledger_outflow(db):
    eid, aid = _seed(db)
    r = apply_buy(db, entity_id=eid, company="AAPL", ticker="AAPL", date=date(2018, 5, 1),
                  unit_price=10.0, shares=1000.0, event_id="b1", account_id=aid)
    assert r["skipped"] is False and r["cost_basis"] == 10000.0
    rows = _holding(db, eid, "AAPL")
    assert len(rows) == 1 and rows[0].event_type == "buy" and rows[0].amount == pytest.approx(1.0)
    led = _ledger_rows(db, aid)
    assert len(led) == 1
    assert led[0].kind == "expense" and led[0].outflow == pytest.approx(10000.0)
    assert "股票事件·b1" in led[0].note


def test_buy_idempotent(db):
    eid, aid = _seed(db)
    k = dict(entity_id=eid, company="AAPL", date=date(2018, 5, 1), unit_price=10.0,
             shares=1000.0, event_id="b1", account_id=aid)
    apply_buy(db, **k)
    r = apply_buy(db, **k)
    assert r["skipped"] is True
    assert len(_holding(db, eid, "AAPL")) == 1
    assert len(_ledger_rows(db, aid)) == 1


# ---------- sell (FIFO) ----------
def _seed_two_batches(db, eid, aid):
    apply_buy(db, entity_id=eid, company="XC", date=date(2000, 1, 1), unit_price=5.0,
              shares=100.0, event_id="b_old", account_id=aid)
    apply_buy(db, entity_id=eid, company="XC", date=date(2001, 1, 1), unit_price=10.0,
              shares=100.0, event_id="b_new", account_id=aid)


def test_sell_fifo_multibatch(db):
    eid, aid = _seed(db)
    _seed_two_batches(db, eid, aid)
    r = apply_sell(db, entity_id=eid, company="XC", date=date(2002, 1, 1), shares=150.0,
                   sell_price=12.0, event_id="s1", account_id=aid)
    assert r["skipped"] is False
    assert len(r["accepted"]) == 2                      # 跨两批
    assert r["cost_basis"] == pytest.approx(5 * 100 + 10 * 50)   # 100@5 + 50@10
    assert r["proceeds"] == pytest.approx(150 * 12)
    assert r["realized_pnl"] == pytest.approx(r["proceeds"] - r["cost_basis"])
    # 原 buy 行递减：old→0(结清)，new→50
    rows = _holding(db, eid, "XC")
    buys = [x for x in rows if x.event_type == "buy"]
    assert abs(float(buys[0].shares) - 0.0) < 1e-9
    assert abs(float(buys[1].shares) - 50.0) < 1e-9
    # sell 行存在
    assert any(x.event_type == "sell" for x in rows)


def test_sell_full_closes_batch_and_value_excludes(db):
    eid, aid = _seed(db)
    _seed_two_batches(db, eid, aid)
    apply_sell(db, entity_id=eid, company="XC", date=date(2002, 1, 1), shares=200.0,
               sell_price=8.0, event_id="s2", account_id=aid)
    assert market_value_at(db, eid, date(2002, 12, 31)) == pytest.approx(0.0)


def test_sell_oversell_422_no_write(db):
    eid, aid = _seed(db)
    _seed_two_batches(db, eid, aid)
    with pytest.raises(ValueError):
        apply_sell(db, entity_id=eid, company="XC", date=date(2002, 1, 1), shares=9999.0,
                   sell_price=8.0, event_id="s3", account_id=aid)
    # 无 sell 行、无新增 ledger（校验在写入前）
    assert not any(x.event_type == "sell" for x in _holding(db, eid, "XC"))
    assert len(_ledger_rows(db, aid)) == 2              # 仅两笔 buy 的 uitflow


def test_sell_ledger_split_principal_plus_pnl(db):
    eid, aid = _seed(db)
    apply_buy(db, entity_id=eid, company="XC", date=date(2000, 1, 1), unit_price=10.0,
              shares=100.0, event_id="bL", account_id=aid)
    r = apply_sell(db, entity_id=eid, company="XC", date=date(2001, 1, 1), shares=100.0,
                   sell_price=14.0, event_id="sL", account_id=aid)
    led = _ledger_rows(db, aid)
    kinds = {x.kind for x in led}
    assert "investment" not in kinds and "pool" not in kinds          # 不与投资池冲突
    assert "income" in kinds and "investment_income" in kinds
    total_in = sum(float(x.inflow or 0) for x in led)
    total_out = sum(float(x.outflow or 0) for x in led)
    # 净现金流 = 卖出现金 − 买入支出 = realized_pnl（+400），非 proceeds 全数
    assert total_in - total_out == pytest.approx(r["realized_pnl"])


# ---------- dividend ----------
def test_dividend_ledger_only_no_holding(db):
    eid, aid = _seed(db)
    apply_buy(db, entity_id=eid, company="AAPL", date=date(2000, 1, 1), unit_price=10.0,
              shares=1000.0, event_id="bd", account_id=aid)
    before = len(_holding(db, eid, "AAPL"))
    r = apply_dividend(db, entity_id=eid, company="AAPL", date=date(2001, 6, 1),
                       per_share=0.5, event_id="d1", account_id=aid)
    assert r["dividend"] == pytest.approx(500.0)
    assert len(_holding(db, eid, "AAPL")) == before            # 不落 holding 行
    led = _ledger_rows(db, aid)
    assert led[-1].kind == "investment_income" and led[-1].inflow == pytest.approx(500.0)


def test_dividend_idempotent(db):
    eid, aid = _seed(db)
    apply_buy(db, entity_id=eid, company="AAPL", date=date(2000, 1, 1), unit_price=10.0,
              shares=1000.0, event_id="bd2", account_id=aid)
    k = dict(entity_id=eid, company="AAPL", date=date(2001, 6, 1), per_share=0.5,
             event_id="d1", account_id=aid)
    apply_dividend(db, **k)
    r = apply_dividend(db, **k)
    assert r["skipped"] is True


# ---------- passive uplift ----------
def test_passive_uplift_no_ledger(db):
    eid, aid = _seed(db)
    apply_buy(db, entity_id=eid, company="AAPL", date=date(2000, 1, 1), unit_price=10.0,
              shares=1000.0, event_id="bu", account_id=aid)
    n_led = len(_ledger_rows(db, aid))
    r = apply_passive_uplift(db, entity_id=eid, company="AAPL", date=date(2012, 12, 30),
                             to_pct=40.0, event_id="pu1")
    assert r["skipped"] is False and r["pct"] == 40.0
    assert len(_ledger_rows(db, aid)) == n_led               # 无 cash 动作
    pseudo = [x for x in _holding(db, eid, "AAPL") if x.event_type == "pseudo"]
    assert len(pseudo) == 1 and float(pseudo[0].pct) == 40.0
    assert float(pseudo[0].shares) == 0.0                    # 不构成 open，避免重复计数
    # 市值不变（被动抬升持股不变）
    assert market_value_at(db, eid, date(2012, 12, 30)) == pytest.approx(10000.0)


def test_market_value_after_buy_sell(db):
    eid, aid = _seed(db)
    apply_buy(db, entity_id=eid, company="AAPL", date=date(2018, 1, 1), unit_price=10.0,
              shares=1000.0, event_id="bv", account_id=aid)
    assert market_value_at(db, eid, date(2018, 12, 30)) == pytest.approx(10000.0)
    apply_sell(db, entity_id=eid, company="AAPL", date=date(2019, 6, 1), shares=400.0,
               sell_price=15.0, event_id="sv", account_id=aid)
    assert market_value_at(db, eid, date(2019, 12, 30)) == pytest.approx(600 * 10.0)
    # 现金 + 市值守恒：初始 0；买→ -10000 现金 +10000 市值；卖→ +6000 现金 +6000 起持仓，盈亏 +2000
    led = _ledger_rows(db, aid)
    cash = sum(float(x.inflow or 0) - float(x.outflow or 0) for x in led)
    assert cash + market_value_at(db, eid, date(2019, 12, 30)) == pytest.approx((15 - 10) * 400)