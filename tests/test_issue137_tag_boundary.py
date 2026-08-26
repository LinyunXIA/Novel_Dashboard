"""issue #137 回归：派生行标签抹除必须词边界精确。

`inv#1` 的 LIKE %inv#1% 会命中 `inv#10~19`——解锁低 id 投资误删高 id 投资的
ledger/finance/timeline 行。delete_derived_by_tag 以正则 `tag(?!\\d)` 复核边界；
demand#{year} 幂等抹除同函数复用。
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import BigInteger, Integer, create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.invest import delete_derived_by_tag, unlock_investment
from app.db import Base
from app.model import (Account, Entity, FinanceEntry, Investment, LedgerEntry,
                       TimelineEvent)


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


def _seed_investment_pair(session):
    e = Entity(entity_type="person", name="Henri Peeters")
    session.add(e)
    session.flush()
    acc = Account(entity_id=e.id, currency="BEF", bank=None)
    session.add(acc)
    inv1 = Investment(id=1, year=1990, region="欧洲", risk_lvl="R3",
                      start_date=date(1990, 6, 1), locked=True)
    inv10 = Investment(id=10, year=1990, region="美国", risk_lvl="R3",
                       start_date=date(1990, 6, 1), locked=True)
    session.add_all([inv1, inv10])
    session.flush()
    # 各自的派生行（note/label 尾随 tag；timeline note 包裹在括号里）
    for inv, outflow in ((inv1, 100), (inv10, 200)):
        tag = f"inv#{inv.id}"
        session.add(LedgerEntry(account_id=acc.id, date=date(1990, 6, 1),
                                reason=f"划入专款池 R3", outflow=outflow,
                                kind="investment", note=f"UI 投资划出 {tag}"))
        session.add(FinanceEntry(entity_id=e.id, entity_kind="person", year=1990,
                                 kind="investment", amount=outflow, currency="BEF",
                                 label=f"投资 R3 {tag}", source="ui"))
        session.add(TimelineEvent(event_year=1990, title=f"投资 {tag}",
                                  note=f"投入专款池，年末赎回（{tag}）",
                                  decade="1990s", overlay=True))
    session.flush()
    return inv1, inv10


def test_unlock_inv1_keeps_inv10_derived_rows(session):
    inv1, inv10 = _seed_investment_pair(session)

    unlock_investment(session, inv1)

    notes = {n for (n,) in session.execute(select(LedgerEntry.note))}
    labels = {n for (n,) in session.execute(select(FinanceEntry.label))}
    titles = {t for (t,) in session.execute(select(TimelineEvent.title))}
    # inv#1 三类行全灭；inv#10 的派生行完好无损
    assert notes == {"UI 投资划出 inv#10"}
    assert labels == {"投资 R3 inv#10"}
    assert titles == {"投资 inv#10"}
    # inv10 本体未动
    assert inv10.locked is True


def test_demand_tag_boundary_exact_year(session):
    e = Entity(entity_type="person", name="Henri Peeters")
    session.add(e)
    session.flush()
    acc = Account(entity_id=e.id, currency="BEF", bank=None)
    session.add(acc)
    session.flush()
    session.add(LedgerEntry(account_id=acc.id, date=date(2024, 12, 30),
                            reason="活期结息", inflow=1, kind="income",
                            note="UI 活期结息 demand#2024"))
    session.add(LedgerEntry(account_id=acc.id, date=date(2023, 12, 30),
                            reason="活期结息", inflow=1, kind="income",
                            note="UI 活期结息 demand#202"))
    session.flush()

    # 抹除 2024 年：不得波及 demand#202（词边界）
    delete_derived_by_tag(session, tag="demand#2024")
    notes = {n for (n,) in session.execute(select(LedgerEntry.note))}
    assert notes == {"UI 活期结息 demand#202"}

    delete_derived_by_tag(session, tag="demand#202")
    assert session.execute(select(LedgerEntry.note)).all() == []
