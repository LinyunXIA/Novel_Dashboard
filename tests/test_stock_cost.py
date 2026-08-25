"""F-P2-03 事件·股票：并购/分拆三形态成本随链引擎单测（§19.6）。

覆盖：纯函数三形态（UTC 分拆/2‑for‑1/MVL‑DIS/纯现金）+ apply_merger DB 写入
（新批次、结清旧行、现金 ledger、幂等）。
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.stock_cost import (apply_merger, cash_merger, cash_share_position,
                                 split_position)
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


def _seed_position(db, entity_id=5, company="UTX", shares=48053700, unit_price=5.0, n_batch=3):
    import app.core.stock_cost as SC
    nid = SC._next_batch_ids(db, n_batch)
    for _ in range(n_batch):
        db.add(HoldingEvent(entity_id=entity_id, company=company, date=date(2000, 12, 31),
                            event_type="buy", batch_id=next(nid),
                            shares=shares / n_batch, unit_price=unit_price,
                            amount=(shares / n_batch) * unit_price / 10000.0))


# -------- 纯函数 --------
def test_pure_split_utc():
    new = split_position([{"shares": 48053700, "unit_price": 5.0}],
                         [{"company": "CARR", "per_old_share": 1.0},
                          {"company": "OTIS", "per_old_share": 0.5},
                          {"company": "RTX", "per_old_share": 1.0}])
    by = {n["company"]: n for n in new}
    assert by["CARR"]["shares"] == 48053700
    assert by["OTIS"]["shares"] == 24026850
    assert by["RTX"]["shares"] == 48053700
    # 成本按股数比 1:0.5:1 摊
    total = sum(n["shares"] * n["unit_price"] for n in new)
    assert total == pytest.approx(48053700 * 5.0)
    assert by["CARR"]["unit_price"] == pytest.approx(by["RTX"]["unit_price"])
    assert by["OTIS"]["unit_price"] == pytest.approx(by["CARR"]["unit_price"])  # 同单价(按股数比自动)


def test_pure_2for1():
    new = split_position([{"shares": 100, "unit_price": 10.0}],
                         [{"company": "X", "per_old_share": 2.0}])
    assert new[0]["shares"] == 200 and new[0]["unit_price"] == 5.0          # 股数×2、成本单价÷2
    assert new[0]["shares"] * new[0]["unit_price"] == 1000.0                # 总成本不变


def test_pure_cash_share_mvl_dis():
    new, cash = cash_share_position([{"shares": 20000000, "unit_price": 5.0}],
                                    [{"company": "DIS", "per_old_share": 0.7452}], 30.0)
    assert new[0]["company"] == "DIS" and new[0]["shares"] == pytest.approx(14904000.0)
    assert cash == 600000000.0                                               # 20M×30
    assert new[0]["shares"] * new[0]["unit_price"] == pytest.approx(100000000.0)  # 成本全额随链


def test_pure_cash_merger():
    assert cash_merger([{"shares": 100000, "unit_price": 1.0}], 30.0) == 3000000.0


# -------- DB apply_merger --------
def test_apply_split_writes_batches_and_closes(db):
    _seed_position(db)
    ent = Entity(id=5, entity_type="person", name="Stijn")
    db.add(ent); db.commit()
    spec = {"entity_id": 5, "date": "2020-04-03", "old_company": "UTX", "form": "split",
            "legs": [{"company": "CARR", "per_old_share": 1.0},
                     {"company": "OTIS", "per_old_share": 0.5},
                     {"company": "RTX", "per_old_share": 1.0}]}
    r = apply_merger(db, spec); db.commit()
    assert r["new_batches"] and r["closed"] == 3
    new = db.execute(select(HoldingEvent).where(HoldingEvent.company.in_(["CARR", "OTIS", "RTX"]))).scalars().all()
    assert len(new) == 9                                        # 3批×3腿，保持 per-batch FIFO 粒度
    from collections import defaultdict
    tot = defaultdict(float)
    for n in new:
        tot[n.company] += float(n.shares)
    assert tot["CARR"] == 48053700 and tot["OTIS"] == 24026850
    # 旧 UTX 结清（标记 closed_on 而非销毁 shares，保留重构前年份市值历史）
    old = db.execute(select(HoldingEvent).where(HoldingEvent.company == "UTX")).scalars().all()
    assert all(o.closed_on is not None for o in old)
    # 幂等：再 apply → skipped
    r2 = apply_merger(db, spec); db.commit()
    assert r2["skipped"] is True


def test_apply_cash_share_writes_ledger_cash(db):
    _seed_position(db, entity_id=9, company="MVL", shares=20000000, unit_price=5.0, n_batch=1)
    db.add_all([Entity(id=9, entity_type="company", name="Peeters Americas"),
                Account(id=12, entity_id=9, currency="USD")]); db.commit()
    spec = {"entity_id": 9, "date": "2009-01-01", "old_company": "MVL", "form": "cash_share",
            "legs": [{"company": "DIS", "per_old_share": 0.7452}],
            "cash_per_share": 30.0, "cash_account_id": 12}
    r = apply_merger(db, spec); db.commit()
    dis = db.execute(select(HoldingEvent).where(HoldingEvent.company == "DIS")).scalar_one()
    assert float(dis.shares) == pytest.approx(14904000.0)
    assert float(dis.shares) * float(dis.unit_price) == pytest.approx(100000000.0)  # 成本全随链
    l = db.execute(select(LedgerEntry).where(LedgerEntry.account_id == 12)).scalar_one()
    assert float(l.inflow) == 600000000.0 and l.kind == "investment_income"


def test_apply_cash_merger_no_ledger_expense(db):
    _seed_position(db, entity_id=3, company="PER", shares=100000, unit_price=1.0, n_batch=1)
    db.add_all([Entity(id=3, entity_type="company", name="Perspecta"),
                Account(id=7, entity_id=3, currency="USD")]); db.commit()
    spec = {"entity_id": 3, "date": "2020-01-01", "old_company": "PER", "form": "cash",
            "cash_per_share": 30.0, "cash_account_id": 7}
    r = apply_merger(db, spec); db.commit()
    assert r["cash"] == 3000000.0 and r["new_batches"] == []
    led = db.execute(select(LedgerEntry).where(LedgerEntry.account_id == 7)).scalar_one()
    assert float(led.inflow) == 3000000.0 and led.kind == "investment_income"  # 进余额、无 expense/损益