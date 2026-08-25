"""F-P2-04 事件·股票：HP_CSC 重组链闭合验证（§19.6，依 H2 逐行验证）。"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.hp_csc_chain import (HP_CSC, HP_CSC_DXC, HP_CSC_DXC_EXPECTED,
                                   HP_CSC_HPE_MFGP_OTEX, HP_CSC_HPINC)
from app.core.stock_chain import apply_chain, verify_chain
from app.core.stock_wealth import market_value_at
from app.db import Base
from app.model import Account, Entity, HoldingEvent


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


def _run_main(db, eid, aid):
    chain = dict(HP_CSC_DXC)
    chain["entity_id"] = eid
    r = apply_chain(db, chain, accounts={"buy": aid, "cash": aid}, commit=True)
    assert r["applied"] == 14 and r["skipped"] == 0
    return chain


def _open(db, eid, company):
    return sum(float(h.shares) for h in db.execute(select(HoldingEvent).where(
        HoldingEvent.entity_id == eid, HoldingEvent.company == company,
        HoldingEvent.shares > 0, HoldingEvent.closed_on.is_(None))).scalars())


def test_hp_csc_chain_closes_to_anchors(db):
    eid, aid = _seed(db)
    chain = _run_main(db, eid, aid)
    v = verify_chain(db, chain, HP_CSC_DXC_EXPECTED, date(2025, 12, 31))
    assert v["ok"] is True, v["problems"]
    by = {r["company"]: r for r in v["rows"]}
    assert by["DXC"]["actual"] == pytest.approx(8_782_400.0, abs=1)
    assert by["OTEX"]["actual"] == pytest.approx(1_227_944.0, abs=1)
    for c in ("PRSP", "CSRA", "MFGP", "CSC", "HPE", "CPQ"):
        assert by[c]["note"] == "closed"
    # 现金三笔
    cash = {c["key"]: c for c in v["cash"]}
    assert cash["并购现金对价·CSRA"]["match"]
    assert cash["并购现金对价·PRSP"]["match"]
    assert cash["并购现金对价·MFGP"]["match"]
    # 总现金对价（CSRA + PRSP + MFGP）
    total = sum(c["actual"] for c in v["cash"])
    assert total == pytest.approx(454_900_875.0 + 127_564_360.0 + 64_050_163.45, rel=1e-6)


def test_hp_csc_chain_asof_midpoint(db):
    from app.core.hp_csc_chain import CPQ_UNIT
    eid, aid = _seed(db)
    chain = _run_main(db, eid, aid)
    # 建仓年（2001，CPQ→HPQ 之前）：CPQ 市值 = 成本；2011：CPQ 已换股为 HPQ，成本随链不变
    cost = 87_474_700.0 * CPQ_UNIT
    assert market_value_at(db, eid, date(2001, 12, 31)) == pytest.approx(cost, rel=1e-4)
    assert market_value_at(db, eid, date(2011, 12, 31)) == pytest.approx(cost, rel=1e-4)
    # CPQ 是已结清的头寸（换股为 HPQ），open 为 0，不被未来重复计数
    assert _open(db, eid, "CPQ") == pytest.approx(0.0)


def test_hp_csc_csc_midyears_sells(db):
    eid, aid = _seed(db)
    _run_main(db, eid, aid)
    # CSC 减持 6/7 步后精确走到 10,578,800（分拆到 DXC 前）
    sql = select(HoldingEvent).where(HoldingEvent.company == "CSC",
                                     HoldingEvent.shares > 0,
                                     HoldingEvent.closed_on.is_(None))
    csc_open = sum(float(h.shares) for h in db.execute(sql).scalars())
    assert csc_open == pytest.approx(0.0)   # CSC 已并入 DXC
    # DXC 中来自 CSC 源的部分 = 8,782,400（HPE 源被 FIFO 卖光）
    dxc = db.execute(select(HoldingEvent).where(
        HoldingEvent.company == "DXC", HoldingEvent.shares > 0,
        HoldingEvent.closed_on.is_(None))).scalars().all()
    assert sum(float(h.shares) for h in dxc) == pytest.approx(8_782_400.0, abs=1)


def test_hp_csc_consolidated_fifo(db):
    eid, aid = _seed(db)
    _run_main(db, eid, aid)
    # DXC open 批次：HPE 源（batch_id 更小）被 FIFO 卖光（shares≈0 或 closed）；CSC 源余 8,782,400
    dxc = db.execute(select(HoldingEvent).where(
        HoldingEvent.company == "DXC").order_by(HoldingEvent.batch_id)).scalars().all()
    open_b = [h for h in dxc if h.closed_on is None and h.shares > 0]
    closed_b = [h for h in dxc if h.closed_on is not None]
    assert closed_b                                        # 有被卖光/结清的 DXC 批次（HPE 源）
    assert len(open_b) >= 1 and sum(float(h.shares) for h in open_b) == pytest.approx(8_782_400.0, abs=1)


def test_hp_csc_subchains_readonly(db):
    eid, aid = _seed(db)
    chain = _run_main(db, eid, aid)
    # 只读子链：不产生新 holding，仅复验主链已建 HPQ / OTEX
    n_hold_before = len(db.execute(select(HoldingEvent)).scalars().all())
    hpq = dict(HP_CSC_HPINC); hpq["entity_id"] = eid
    assert verify_chain(db, hpq, HP_CSC_HPINC["expected"], date(2025, 12, 31))["ok"]
    otex = dict(HP_CSC_HPE_MFGP_OTEX); otex["entity_id"] = eid
    assert verify_chain(db, otex, HP_CSC_HPE_MFGP_OTEX["expected"], date(2025, 12, 31))["ok"]
    assert len(db.execute(select(HoldingEvent)).scalars().all()) == n_hold_before