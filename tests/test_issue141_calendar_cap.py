"""issue #141 回归：日历年上限收敛 app.config 单一来源，跨入上限年不再静默停滚。

此前 range(from_year, 2026)（不含 2026）散落 leverage/snapshot/main 等十余处，
余额滚动实际止于 2025。本文件验证：
1. config 常量动态（>=2026 且随当前年推进）；
2. leverage.recompute_one 能滚动到（monkeypatch 后的）上限年并回填年末余额；
3. 各模块引用的上限与 config 一致。
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import BigInteger, Integer, create_engine, select
from sqlalchemy.orm import sessionmaker

import app.core.leverage as leverage_mod
from app.config import CALENDAR_MAX_YEAR, calendar_years
from app.core.leverage import recompute_one
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


def test_config_bounds_dynamic():
    import datetime
    assert CALENDAR_MAX_YEAR >= 2026
    assert CALENDAR_MAX_YEAR == max(2026, datetime.date.today().year + 1)
    assert min(calendar_years()) == 1947
    assert max(calendar_years()) == CALENDAR_MAX_YEAR


@pytest.mark.parametrize("mod", ["app.api.ui_ops", "app.api.timeline",
                                 "app.api.stock_events", "app.core.transfer"])
def test_module_caps_follow_config(mod):
    import importlib
    m = importlib.import_module(mod)
    val = getattr(m, "_YEAR_MAX", None) or getattr(m, "_MAX_YEAR", None)
    assert val == CALENDAR_MAX_YEAR, f"{mod} 上限未跟随 config"


def test_leverage_rolls_through_cap_year(session, monkeypatch):
    """上限年有流水+收益值时，年末余额必须被复利回填（修复前循环止于 2025）。"""
    monkeypatch.setattr(leverage_mod, "CALENDAR_MAX_YEAR", 2028)
    e = Entity(entity_type="person", name="Investor",
               fields={"compound": True, "return_region": "欧洲", "risk_lvl": "R3"})
    session.add(e)
    session.flush()
    acc = Account(entity_id=e.id, currency="EUR", bank=None)
    session.add(acc)
    session.flush()
    session.add(LedgerEntry(account_id=acc.id, date=date(2025, 12, 30),
                            reason="期初", inflow=1000, balance=1000, kind="income"))
    session.add(LedgerEntry(account_id=acc.id, date=date(2027, 6, 1),
                            reason="年中流入", inflow=100, kind="income"))
    session.add(ReturnCurve(country="欧洲", risk_lvl="R3", year=2027, rate=10.0))
    session.flush()

    res = recompute_one(session, acc.id, from_year=2026)
    assert res["updated"] >= 1
    row = session.execute(select(LedgerEntry).where(
        LedgerEntry.date == date(2027, 6, 1))).scalar_one()
    # 2027 年 rate=10% × 杠杆 2×（1989 起）=20% → 1000×1.2 + 流入 100 = 1300
    assert abs(float(row.balance) - 1300.0) < 1e-6
