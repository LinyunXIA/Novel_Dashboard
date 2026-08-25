"""F-P2-04 事件·股票：链编排器 apply_chain / verify_chain 单测（§19.6 block chain）。"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.stock_chain import apply_chain, verify_chain
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


def _open(db, eid, company):
    return sum(float(h.shares) for h in db.execute(select(HoldingEvent).where(
        HoldingEvent.entity_id == eid, HoldingEvent.company == company,
        HoldingEvent.shares > 0, HoldingEvent.closed_on.is_(None))).scalars())


def test_apply_chain_sorts_and_dispatches(db):
    eid, aid = _seed(db)
    chain = {"name": "t", "entity_id": eid, "steps": [
        {"type": "buy", "company": "AAPL", "date": "2018-06-01", "unit_price": 10.0,
         "shares": 1000.0, "account_id": "@buy"},
        {"type": "split", "company": "AAPL", "date": "2020-01-01",
         "legs": [{"company": "AAPL", "per_old_share": 1.0},
                  {"company": "DEMO", "per_old_share": 0.5}]},
        {"type": "sell", "company": "AAPL", "date": "2019-01-01", "shares": 400.0,
         "sell_price": 15.0, "account_id": "@cash", "calibrated": True},
    ]}
    # 故意乱序（sell 2019 写在 split 2020 前）
    chain["steps"] = [chain["steps"][2], chain["steps"][1], chain["steps"][0]]
    r = apply_chain(db, dict(chain), accounts={"buy": aid, "cash": aid}, commit=True)
    assert r["applied"] == 3 and r["skipped"] == 0
    assert r["calibrated_steps"] == [2]            # sell(2019) 排序后为 seq 2
    try:
        assert _open(db, eid, "AAPL") == pytest.approx(600.0)          # buy 1000 - sell 400
        assert _open(db, eid, "DEMO") == pytest.approx(600 * 0.5)      # split 2020 作用于剩余 600 → DEMO 300
    except AssertionError:
        pytest.fail("chain 未按日期顺序应用")


def test_apply_chain_idempotent_replay(db):
    eid, aid = _seed(db)
    chain = {"name": "t2", "entity_id": eid, "steps": [
        {"type": "buy", "company": "AAPL", "date": "2018-06-01", "unit_price": 10.0,
         "shares": 1000.0, "account_id": "@buy"},
        {"type": "sell", "company": "AAPL", "date": "2019-01-01", "shares": 400.0,
         "sell_price": 15.0, "account_id": "@cash"},
    ]}
    apply_chain(db, dict(chain), accounts={"buy": aid, "cash": aid}, commit=True)
    nh = len(db.execute(select(HoldingEvent)).scalars().all())
    nl = len(db.execute(select(LedgerEntry)).scalars().all())
    r2 = apply_chain(db, dict(chain), accounts={"buy": aid, "cash": aid}, commit=True)
    assert r2["applied"] == 0 and r2["skipped"] == 2
    assert len(db.execute(select(HoldingEvent)).scalars().all()) == nh
    assert len(db.execute(select(LedgerEntry)).scalars().all()) == nl
    v = verify_chain(db, dict(chain), [{"company": "AAPL", "shares": 600.0, "open": True}],
                     date(2020, 12, 31))
    assert v["ok"]


def test_verify_chain_shares_and_closed(db):
    eid, aid = _seed(db)
    chain = {"name": "t3", "entity_id": eid, "steps": [
        {"type": "buy", "company": "AAPL", "date": "2018-06-01", "unit_price": 10.0,
         "shares": 1000.0, "account_id": "@buy"},
        {"type": "sell", "company": "AAPL", "date": "2019-01-01", "shares": 1000.0,
         "sell_price": 15.0, "account_id": "@cash"},
    ]}
    apply_chain(db, dict(chain), accounts={"buy": aid, "cash": aid}, commit=True)
    v = verify_chain(db, dict(chain),
                     [{"company": "AAPL", "shares": 0.0, "open": False}], date(2020, 12, 31))
    assert v["ok"]
    rows = {r["company"]: r for r in v["rows"]}
    assert rows["AAPL"]["note"] == "closed"


def test_verify_chain_cash_from_reason(db):
    eid, aid = _seed(db)
    chain = {"name": "t4", "entity_id": eid, "steps": [
        {"type": "buy", "company": "PER", "date": "2018-01-01", "unit_price": 1.0,
         "shares": 100.0, "account_id": "@buy"},
        {"type": "cash", "company": "PER", "date": "2020-01-01", "cash_per_share": 30.0,
         "cash_account_id": "@cash"},
    ]}
    apply_chain(db, dict(chain), accounts={"buy": aid, "cash": aid}, commit=True)
    v = verify_chain(db, dict(chain),
                     [{"ledger_kind": "investment_income", "reason_like": "并购现金对价·PER",
                       "amount": 3000.0}], date(2020, 12, 31))
    assert v["ok"] and v["cash"][0]["match"]


def test_verify_chain_mismatch_flags_problem(db):
    eid, aid = _seed(db)
    chain = {"name": "t5", "entity_id": eid, "steps": [
        {"type": "buy", "company": "AAPL", "date": "2018-06-01", "unit_price": 10.0,
         "shares": 1000.0, "account_id": "@buy"},
    ]}
    apply_chain(db, dict(chain), accounts={"buy": aid, "cash": aid}, commit=True)
    v = verify_chain(db, dict(chain), [{"company": "AAPL", "shares": 9999.0, "open": True}],
                     date(2020, 12, 31))
    assert v["ok"] is False
    assert any("AAPL" in p for p in v["problems"])
    rows = {r["company"]: r for r in v["rows"]}
    assert rows["AAPL"]["match"] is False


def test_verify_chain_unasserted_company(db):
    eid, aid = _seed(db)
    chain = {"name": "t6", "entity_id": eid, "steps": [
        {"type": "buy", "company": "AAPL", "date": "2018-06-01", "unit_price": 10.0,
         "shares": 1000.0, "account_id": "@buy"},
    ]}
    apply_chain(db, dict(chain), accounts={"buy": aid, "cash": aid}, commit=True)
    # expected 里没有 AAPL → 标 unasserted，但不进 problems
    v = verify_chain(db, dict(chain), [{"company": "OTHER", "shares": 0.0, "open": False}],
                     date(2020, 12, 31))
    assert v["ok"] is True
    assert any(r["company"] == "AAPL" and r["note"] == "unasserted" for r in v["rows"])


def test_split_preserves_parent_same_name_leg(db):
    eid, aid = _seed(db)
    chain = {"name": "t7", "entity_id": eid, "steps": [
        {"type": "buy", "company": "HPQ", "date": "2015-06-01", "unit_price": 10.0,
         "shares": 1000.0, "account_id": "@buy"},
        {"type": "split", "company": "HPQ", "date": "2015-11-01",
         "legs": [{"company": "HPQ", "per_old_share": 1.0},
                  {"company": "HPE", "per_old_share": 1.0}]},
    ]}
    apply_chain(db, dict(chain), accounts={"buy": aid, "cash": aid}, commit=True)
    # 同名腿 HPQ 保留、HPE 生成；旧 buy 行已结清
    assert _open(db, eid, "HPQ") == pytest.approx(1000.0)
    assert _open(db, eid, "HPE") == pytest.approx(1000.0)
    hpq = db.execute(select(HoldingEvent).where(
        HoldingEvent.company == "HPQ")).scalars().all()
    assert any(h.closed_on is not None for h in hpq)      # 旧 buy 已结清
    assert any(h.closed_on is None and h.event_type == "split" for h in hpq)  # 同名腿 open