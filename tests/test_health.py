"""Unit tests for app/core/health（issue #22 回归）。

覆盖：
- H4 首条校验（i=0 起）：balance != inflow-outflow 应报错
- H4 链式连续性：i>0 时 prev.balance + in - out 不等报错
- H3 对称 NULL 回退：direct 缺失时不应误报（issue #22 修复点）
- 负余额检查：balance < 0 时 warn
- 汇率表空检查：exchange_rate 为空时 warn
- 汇率表全 NULL year 检查：仅基准常量折算时 warn
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.health import (
    check_fx_coverage, check_h3_fx_closure, check_h4_balance_chain,
    check_negative_balance, run_report,
)
from app.db import Base
from app.model import Account, Entity, ExchangeRate, LedgerEntry


@pytest.fixture
def session():
    """内存 SQLite + 临时 DDL；BigInteger PK → Integer 兼容 SQLite autoincrement。"""
    from sqlalchemy import BigInteger, Integer

    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def _seed_entity(session, name="Henri Peeters") -> Entity:
    e = Entity(entity_type="person", name=name)
    session.add(e)
    session.flush()
    return e


def _seed_account(session, entity: Entity, currency="BEF") -> Account:
    a = Account(entity_id=entity.id, currency=currency)
    session.add(a)
    session.flush()
    return a


class TestH4FirstEntryChecked:
    def test_first_entry_mismatch_reports(self, session):
        """issue #22 修复点：i=0 首条 balance != inflow-outflow 必须报错。

        此前因 for i in range(1, ...) 跳过首条，导致首条失核逃逸。
        """
        e = _seed_entity(session)
        a = _seed_account(session, e)
        # 首条：inflow=100, outflow=20, balance=1000（应等于 80）
        session.add(LedgerEntry(account_id=a.id, date=date(1990, 1, 1),
                                inflow=100, outflow=20, balance=1000))
        session.commit()
        finds = check_h4_balance_chain(session)
        crit = [f for f in finds if f.level == "crit"]
        assert len(crit) == 1, f"首条失核必须报错；got {crit}"
        assert "首条" in crit[0].detail

    def test_first_entry_consistent_passes(self, session):
        """首条 balance == inflow - outflow 时不报错。"""
        e = _seed_entity(session)
        a = _seed_account(session, e)
        session.add(LedgerEntry(account_id=a.id, date=date(1990, 1, 1),
                                inflow=100, outflow=20, balance=80))
        session.commit()
        finds = check_h4_balance_chain(session)
        assert finds == [], f"首条合法应零报错；got {finds}"

    def test_chain_break_after_first(self, session):
        """链式连续性：第二条开始按 prev.balance + in - out 校验。"""
        e = _seed_entity(session)
        a = _seed_account(session, e)
        session.add_all([
            LedgerEntry(account_id=a.id, date=date(1990, 1, 1), inflow=100, balance=100),
            # 第二条：应=100+50-10=140，写成 200 应报错
            LedgerEntry(account_id=a.id, date=date(1990, 6, 1), inflow=50, outflow=10, balance=200),
        ])
        session.commit()
        finds = check_h4_balance_chain(session)
        crit = [f for f in finds if "余额" in f.detail]
        assert any("200" in f.detail and "140" in f.detail for f in crit), \
            f"链式断链未报错；got {crit}"


class TestH3SymmetricNullFallback:
    def test_direct_missing_no_false_alarm(self, session):
        """issue #22 修复点：direct 侧缺值时不应误报（H3 链式 vs 直接）。"""
        # A→B 和 B→C 都有，direct A→C 没有
        session.add_all([
            ExchangeRate(fx_from="USD", fx_to="EUR", year=2000, rate=0.9),
            ExchangeRate(fx_from="EUR", fx_to="BEF", year=2000, rate=40.34),
            # 故意无 USD→BEF direct
        ])
        session.commit()
        finds = check_h3_fx_closure(session)
        assert finds == [], f"direct 缺失不应误报；got {finds}"

    def test_three_way_break_reports(self, session):
        """三向都有但不一致 → crit。"""
        session.add_all([
            ExchangeRate(fx_from="USD", fx_to="EUR", year=2000, rate=0.9),
            ExchangeRate(fx_from="EUR", fx_to="BEF", year=2000, rate=40.0),  # 链式 36.0
            ExchangeRate(fx_from="USD", fx_to="BEF", year=2000, rate=40.0),  # direct 40.0
        ])
        session.commit()
        finds = check_h3_fx_closure(session)
        assert any(f.rule == "H3" and f.level == "crit" for f in finds)


class TestNegativeBalance:
    def test_negative_balance_warns(self, session):
        """issue #22：负余额 warn（H4 级别，但 level=warn 不阻断）。"""
        e = _seed_entity(session)
        a = _seed_account(session, e)
        session.add(LedgerEntry(account_id=a.id, date=date(1990, 1, 1),
                                inflow=50, outflow=100, balance=-50))
        session.commit()
        finds = check_negative_balance(session)
        assert len(finds) == 1
        assert finds[0].level == "warn"
        assert "-50" in finds[0].detail

    def test_positive_balance_no_warn(self, session):
        session.add(LedgerEntry(account_id=_seed_account(session, _seed_entity(session)).id,
                                date=date(1990, 1, 1), inflow=100, balance=100))
        session.commit()
        assert check_negative_balance(session) == []


class TestFxCoverage:
    def test_empty_table_warns(self, session):
        """issue #22：exchange_rate 为空 → warn（USD 折算空转不可见）。"""
        finds = check_fx_coverage(session)
        assert len(finds) == 1
        assert finds[0].level == "warn"
        assert "空" in finds[0].detail

    def test_all_null_year_warns(self, session):
        """全部 year=NULL → warn（仅基准常量折算，无逐年）。"""
        session.add(ExchangeRate(fx_from="USD", fx_to="EUR", year=None, rate=0.9))
        session.commit()
        finds = check_fx_coverage(session)
        assert len(finds) == 1
        assert "常量" in finds[0].detail

    def test_mixed_years_no_warn(self, session):
        """有 year 不全 NULL → 不报（覆盖足够）。"""
        session.add_all([
            ExchangeRate(fx_from="USD", fx_to="EUR", year=2000, rate=0.9),
            ExchangeRate(fx_from="USD", fx_to="EUR", year=None, rate=0.85),
        ])
        session.commit()
        assert check_fx_coverage(session) == []


class TestRunReportIntegration:
    def test_all_checks_compose(self, session):
        """run_report 汇总所有 check，无报错时返回空列表。"""
        e = _seed_entity(session)
        a = _seed_account(session, e)
        session.add_all([
            LedgerEntry(account_id=a.id, date=date(1990, 1, 1), inflow=100, balance=100),
            ExchangeRate(fx_from="USD", fx_to="EUR", year=2000, rate=0.9),
        ])
        session.commit()
        report = run_report(session)
        # 期望：H3 覆盖率无 warn、H4 无 crit、负余额无、其余规则无匹配
        assert report == [], f"干净数据应零发现；got {report}"