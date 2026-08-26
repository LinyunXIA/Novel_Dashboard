"""issue #113 回归：地区/币种映射对齐 return_curve 真实国家名。

旧 REGION_COUNTRY 把 欧洲→比利时、香港→中国香港、中国→中国大陆，
而库中（源 5 份地区表）只有 欧洲/英国/美国/香港/中国 → 收益查询恒 None：
重算从不复利、投资对真实数据 422。本文件钉死 identity 映射与覆盖钩子。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import BigInteger, create_engine, Integer
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.leverage import _leverage_for_year, _rate_for_account_year, recompute_one
from app.core.regions import (
    CURRENCY_REGION, DEFAULT_RISK_LVL, REGION_COUNTRY,
    entity_region_override, entity_risk_override,
)
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


_SEQ = {"n": 0}


def _acct(session, currency: str, fields: dict | None = None) -> Account:
    _SEQ["n"] += 1
    e = Entity(entity_type="person", name=f"测试-{currency}-{_SEQ['n']}", fields=fields or {})
    a = Account(entity_id=0, currency=currency)
    session.add_all([e, a])
    session.flush()
    a.entity_id = e.id
    session.commit()
    return a


def test_region_country_is_identity_and_matches_source_tables():
    assert REGION_COUNTRY == {
        "欧洲": "欧洲", "英国": "英国", "美国": "美国", "香港": "香港", "中国": "中国",
    }


def test_currency_region_covers_family_currencies():
    for cur in ("BEF", "LUF", "EUR", "SEK", "DKK", "NLG"):
        assert CURRENCY_REGION[cur] == "欧洲"
    assert CURRENCY_REGION["GBP"] == "英国"
    assert CURRENCY_REGION["USD"] == "美国"
    assert CURRENCY_REGION["HKD"] == "香港"
    assert CURRENCY_REGION["CNY"] == "中国"
    assert CURRENCY_REGION.get("XYZ") is None


_COMPOUND = {"compound": True}


def test_rate_is_none_without_compound_opt_in(session):
    """口径定案 A：未 opt-in 的源台账账户一律不复利（文件即权威）。"""
    a = _acct(session, "BEF")
    session.add(ReturnCurve(country="欧洲", risk_lvl="R3", year=1990, rate=21.7))
    session.commit()
    assert _rate_for_account_year(session, a, 1990) is None
    b = _acct(session, "BEF", fields={"compound": False})
    assert _rate_for_account_year(session, b, 1990) is None


def test_rate_lookup_hits_europe_curve_for_bef_account(session):
    """回归核心：opt-in 账户按「欧洲」R3 查到收益率（旧实现查「比利时」恒 None）。"""
    a = _acct(session, "BEF", fields=_COMPOUND)
    session.add(ReturnCurve(country="欧洲", risk_lvl="R3", year=1990, rate=21.7))
    session.commit()
    rate = _rate_for_account_year(session, a, 1990)
    assert rate is not None
    # 21.7% × 1989 起 2 倍杠杆
    assert rate == (Decimal("0.217") * _leverage_for_year(1990)).quantize(Decimal("0.000001"))


@pytest.mark.parametrize("cur,country", [
    ("GBP", "英国"), ("USD", "美国"), ("HKD", "香港"), ("CNY", "中国"),
])
def test_rate_lookup_per_region(session, cur, country):
    a = _acct(session, cur, fields=_COMPOUND)
    session.add(ReturnCurve(country=country, risk_lvl="R3", year=2005, rate=10.0))
    session.commit()
    assert _rate_for_account_year(session, a, 2005) == Decimal("0.2")  # 10% × 2×


def test_entity_fields_overrides(session):
    a = _acct(session, "BEF", fields={"compound": True, "return_region": "美国", "risk_lvl": "R5"})
    session.add(ReturnCurve(country="美国", risk_lvl="R5", year=2010, rate=30.0))
    session.commit()
    assert entity_region_override({"return_region": "美国"}) == "美国"
    assert entity_risk_override({"risk_lvl": "R5"}) == "R5"
    assert entity_risk_override({"risk_lvl": "R9"}) is None  # 非法值回退默认
    rate = _rate_for_account_year(session, a, 2010)
    assert rate == (Decimal("0.30") * Decimal("2.0")).quantize(Decimal("0.000001"))
    assert DEFAULT_RISK_LVL == "R3"


def test_recompute_one_compounds_with_leverage(session):
    """端到端：1975 年 100 本金、2000 年一条零流水分录作落点，按 欧洲 R3=10% 复利。

    1976-1988 用 1.5×：每年 ×(1+0.15)；1989 起用 2×：每年 ×(1+0.20)。
    """
    a = _acct(session, "BEF", fields=_COMPOUND)
    session.add(LedgerEntry(account_id=a.id, date=date(1975, 12, 30),
                            inflow=100, balance=100))
    session.add(LedgerEntry(account_id=a.id, date=date(2000, 6, 30), inflow=0))
    for y in range(1976, 2027):
        session.add(ReturnCurve(country="欧洲", risk_lvl="R3", year=y, rate=10.0))
    session.commit()

    res = recompute_one(session, a.id, from_year=1976)
    assert res["updated"] > 0

    bal = Decimal("100")
    for y in range(1976, 2001):
        lev = Decimal("1.5") if y < 1989 else Decimal("2.0")
        bal = bal * (1 + Decimal("0.10") * lev)
    last = session.query(LedgerEntry).filter_by(account_id=a.id).order_by(
        LedgerEntry.date.desc(), LedgerEntry.id.desc()).first()
    assert Decimal(last.balance).quantize(Decimal("0.01")) == bal.quantize(Decimal("0.01"))
