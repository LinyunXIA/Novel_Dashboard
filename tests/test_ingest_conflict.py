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
from app.model import Account, Entity, IncomeStream, LedgerEntry
from app.ingest.conflict import (
    check_account_balance_conflict,
    check_bank_import_conflict,
    check_income_stream_conflict,
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