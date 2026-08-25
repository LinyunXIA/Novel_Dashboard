"""Unit tests for ingest 幂等/冲突总结（issue #14/#15 回归）。

- conflict.check_income_stream_conflict 输出 H2/H5 明细（而非只留计数）
- conflict.check_bank_import_conflict 对既有 account 做 H4 余额断链
- conflict.check_income_stream_conflict 不是 main.py 复制版（H5 引用明细）
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.model import Account, Entity, HoldingEvent, IncomeStream, LedgerEntry, StockEvent
from app.ingest.conflict import (
    check_account_balance_conflict,
    check_bank_import_conflict,
    check_income_stream_conflict,
    check_stock_event_conflict,
)
from app.ingest.main import _normalize_conflict_recs


@pytest.fixture
def session():
    from sqlalchemy import BigInteger, Integer

    def _patch():
        for table in Base.metadata.tables.values():
            for col in table.columns:
                if isinstance(col.type, BigInteger) and col.primary_key:
                    col.type = Integer()

    _patch()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


class TestIncomeStreamConflictDetail:
    """issue #15：冲突明细（规则/行/新旧值）可原样输出，不被缩成计数。"""

    def test_h2_amount_conflict_reported_with_details(self, session):
        h = Entity(entity_type="person", name="Henri Peeters")
        session.add(h)
        session.flush()
        session.add(IncomeStream(entity_id=h.id, stream_type="security", group_key="骨", currency="BEF",
                                 year=2000, amount=100.0, label="既有"))
        session.commit()
        recs = _normalize_conflict_recs("income_security", [{
            "holder": "Henri Peeters", "name": "债", "face_value": 2000.0,
            "rate_pct": 5.0, "currency": "BEF",
        }])
        # 面值2000×5%=100 与既有100一致 → 无冲突
        rep = check_income_stream_conflict(session, "收益.md", recs)
        assert rep.blocked is False

    def test_h2_conflict_blocked(self, session):
        h = Entity(entity_type="person", name="Henri Peeters")
        session.add(h)
        session.flush()
        session.add(IncomeStream(entity_id=h.id, stream_type="salary", group_key="薪", currency="BEF",
                                 year=2000, amount=100.0, label="既有"))
        session.commit()
        recs = _normalize_conflict_recs("salary", [{
            "holder": "Henri Peeters", "year": 2000, "currency": "BEF", "after_tax": 999.0,
        }])
        rep = check_income_stream_conflict(session, "工资.md", recs)
        assert rep.blocked is True
        assert any(p["rule"] == "H2-金额" and "999.0" in p["detail"] for p in rep.problems)

    def test_h5_reference_reported(self, session):
        recs = _normalize_conflict_recs("salary", [{
            "holder": "不存在的人", "year": 2000, "currency": "BEF", "after_tax": 10.0,
        }])
        rep = check_income_stream_conflict(session, "工资.md", recs)
        # issue #72：H5 按 §11.4 定级为「标」——软警告、不再 hard-block
        assert rep.blocked is False
        assert any(w["rule"] == "H5-引用" and "不存在的人" in w["detail"] for w in rep.warnings)


class TestIncomeStreamConflictYearsAmounts:
    """_normalize_conflict_recs 对齐 income_shop(y0)、salary(after_tax)。"""

    def test_income_shop_y0_mapped(self, session):
        h = Entity(entity_type="person", name="Henri Peeters")
        session.add(h)
        session.flush()
        session.add(IncomeStream(entity_id=h.id, stream_type="shop", group_key="店",
                                 currency="BEF", year=2001, amount=100.0, label="既有"))
        session.commit()
        recs = _normalize_conflict_recs("income_shop", [{
            "holder": "Henri Peeters", "y0": 2001, "y1": 2005, "currency": "BEF",
            "amount": 100.0,
        }])
        rep = check_income_stream_conflict(session, "开店.md", recs)
        assert rep.blocked is False


class TestBankImportConflict:
    """issue #15：bank 导入接 H4/H5。"""

    def test_h4_balance_chain(self, session):
        h = Entity(entity_type="person", name="祖父")
        session.add(h)
        session.flush()
        acc = Account(entity_id=h.id, currency="BEF")
        session.add(acc)
        session.flush()
        session.add(LedgerEntry(account_id=acc.id, date=date(1980, 12, 31),
                                inflow=100, balance=100, kind="income", reason="a"))
        session.commit()
        segs = [{"holder": "祖父", "currency": "BEF", "bank": None, "seg_title": "BEF（祖父）",
                 "rows": [{"date": "1981-01-01", "reason": "b", "inflow": 50, "outflow": None,
                           "balance": 500, "note": None}]}]
        rep = check_bank_import_conflict(session, "祖父.md", segs)
        # 既有末余额 100 ≠ 新首笔 500 → H4
        assert rep.blocked is True
        assert any(p["rule"] == "H4-余额" for p in rep.problems)

    def test_h4_no_conflict_when_balance_matches(self, session):
        h = Entity(entity_type="person", name="祖父")
        session.add(h)
        session.flush()
        acc = Account(entity_id=h.id, currency="BEF")
        session.add(acc)
        session.flush()
        session.add(LedgerEntry(account_id=acc.id, date=date(1980, 12, 31),
                                inflow=100, balance=100, kind="income", reason="a"))
        session.commit()
        segs = [{"holder": "祖父", "currency": "BEF", "bank": None, "seg_title": "BEF",
                 "rows": [{"date": "1981-01-01", "reason": "b", "inflow": 50, "outflow": None,
                           "balance": 100, "note": None}]}]
        rep = check_bank_import_conflict(session, "祖父.md", segs)
        assert rep.blocked is False

    def test_h5_reference(self, session):
        segs = [{"holder": "不存在的人", "currency": "BEF", "bank": None, "seg_title": "BEF",
                 "rows": [{"date": "1981-01-01", "reason": "b", "inflow": 1, "outflow": None,
                           "balance": 1, "note": None}]}]
        rep = check_bank_import_conflict(session, "x.md", segs)
        # issue #72：H5 软警告（标），不拦整文件
        assert rep.blocked is False
        assert any(w["rule"] == "H5-引用" for w in rep.warnings)

    def test_new_account_no_h4(self, session):
        h = Entity(entity_type="person", name="祖父")
        session.add(h)
        session.commit()
        segs = [{"holder": "祖父", "currency": "BEF", "bank": None, "seg_title": "BEF",
                 "rows": [{"date": "1981-01-01", "reason": "b", "inflow": 1, "outflow": None,
                           "balance": 1, "note": None}]}]
        rep = check_bank_import_conflict(session, "x.md", segs)
        assert rep.blocked is False          # 新账户无从 H4

    def test_check_account_balance_conflict_direct(self, session):
        h = Entity(entity_type="person", name="祖父")
        session.add(h)
        session.flush()
        acc = Account(entity_id=h.id, currency="BEF")
        session.add(acc)
        session.flush()
        session.add(LedgerEntry(account_id=acc.id, date=date(1980, 12, 31),
                                inflow=100, balance=100, kind="income", reason="a"))
        session.commit()
        rep = check_account_balance_conflict(session, "x.md", acc.id,
                                             [{"date": "1981-01-01", "balance": 999}])
        assert rep.blocked is True


# ---------- F-P2-04：§11.4 stock 事件冲突 ----------
def _seed_stock(session, *, company, date_, event_type, amount, shares=None):
    session.add(StockEvent(company=company, date=date_, event_type=event_type,
                           amount=amount, shares=shares, source_file="seed.md"))
    session.flush()


def test_stock_event_conflict_block_duplicate_amount(session):
    _seed_stock(session, company="AAPL", date_=date(2018, 6, 1), event_type="buy",
                amount=1000.0, shares=100.0)
    # 新文件同键但金额不同 → hard-block
    rep = check_stock_event_conflict(session, "new.md",
                                     [{"company": "AAPL", "date": "2018-06-01",
                                       "event_type": "buy", "amount": 9999.0, "shares": 100.0}])
    assert rep.blocked is True
    assert any(p["rule"] == "H2-股票" for p in rep.problems)


def test_stock_event_conflict_pass_same_amount(session):
    _seed_stock(session, company="AAPL", date_=date(2018, 6, 1), event_type="buy",
                amount=1000.0, shares=100.0)
    rep = check_stock_event_conflict(session, "new.md",
                                     [{"company": "AAPL", "date": "2018-06-01",
                                       "event_type": "buy", "amount": 1000.0, "shares": 100.0}])
    assert rep.blocked is False


def test_stock_event_conflict_vs_holding_event(session):
    e = Entity(entity_type="person", name="Stijn")
    session.add(e)
    session.flush()
    session.add(HoldingEvent(entity_id=e.id, company="AAPL", date=date(2018, 6, 1),
                             event_type="buy", shares=100.0, unit_price=10.0,
                             amount=0.1, source_file="chain.md"))
    session.flush()
    rep = check_stock_event_conflict(session, "new.md",
                                     [{"entity_id": e.id, "company": "AAPL",
                                       "date": "2018-06-01", "event_type": "buy",
                                       "amount": 0.5, "shares": 100.0}])
    assert rep.blocked is True


def test_stock_event_conflict_same_file_ignored(session):
    """同一 source_file 重导入不算冲突（幂等 upsert 交给 import_stock_events）。"""
    session.add(StockEvent(company="AAPL", date=date(2018, 6, 1), event_type="buy",
                           amount=1000.0, shares=100.0, source_file="快手.md"))
    session.flush()
    rep = check_stock_event_conflict(session, "快手.md",   # 与库中同 source_file
                                     [{"company": "AAPL", "date": "2018-06-01",
                                       "event_type": "buy", "amount": 5000.0, "shares": 200.0}])
    assert rep.blocked is False


def test_stock_event_conflict_gate_uses_blocked(session):
    """events_stock 的 gate 凭 rep.blocked 判：冲突文件记录不应导入。"""
    _seed_stock(session, company="AAPL", date_=date(2018, 6, 1), event_type="buy",
                amount=1000.0, shares=100.0)
    recs = [{"company": "AAPL", "date": "2018-06-01", "event_type": "buy",
             "amount": 2000.0, "shares": 100.0, "source_file": "new.md"}]
    by_file = {}
    for rec in recs:
        by_file.setdefault(rec.get("source_file"), []).append(rec)
    ok, blocked = [], 0
    for src, group in by_file.items():
        crep = check_stock_event_conflict(session, src, group)
        if crep.blocked:
            blocked += 1
        else:
            ok.extend(group)
    assert blocked == 1 and not ok