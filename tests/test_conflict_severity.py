"""issue #72：conflict 软硬分级（§11.4「挡/标」）+ H1 增量瘦版 + H3 链式闭合预检。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.model import Entity, ExchangeRate, TimelineEvent
from app.ingest.conflict import (
    check_fx_chain_closure,
    check_income_stream_conflict,
    check_timeline_alignment,
)
from app.ingest.main import _normalize_conflict_recs as _normalize_recs_for_test


@pytest.fixture()
def session():
    from sqlalchemy import BigInteger, Integer
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


class TestH1TimelineAlignment:
    def _recs(self, *years):
        return [{"entity_name": "Henri Peeters", "stream_type": "shop",
                 "currency": "BEF", "year": y, "amount": 1.0} for y in years]

    def test_missing_year_warns(self, session):
        rep = check_timeline_alignment(session, "店.md", self._recs(1999))
        assert not rep.blocked
        assert any(w["rule"] == "H1-时间线" and w["line"] == 1999 for w in rep.warnings)

    def test_covered_year_clean(self, session):
        session.add(TimelineEvent(event_year=1999, title="开店"))
        session.flush()
        rep = check_timeline_alignment(session, "店.md", self._recs(1999))
        assert rep.warnings == []

    def test_merged_into_income_report_not_blocking(self, session):
        """H1 软警告与 H2 硬冲突并存时：H2 拦、H1 不改变拦截判定。"""
        h = Entity(entity_type="person", name="Henri Peeters")
        session.add(h)
        session.flush()
        recs = _normalize_recs_for_test("income_shop", [{
            "holder": "Henri Peeters", "y0": 2001, "currency": "BEF", "amount": 999.0}])
        rep = check_income_stream_conflict(session, "店.md", recs)
        assert not rep.blocked          # 无既有金额 → 无 H2；H5 亦无（实体存在）
        # 年份 2001 无时间线 → 若合并 H1 只产生 warnings
        rep.merge(check_timeline_alignment(session, "店.md", recs))
        assert not rep.blocked and rep.warnings


class TestH3ChainClosure:
    def test_chain_mismatch_blocks(self, session):
        """DB 有 USD→EUR=2、EUR→BEF=3；新文件直接给 USD→BEF=7（链式 6≠7 >0.5%）→ 挡。"""
        session.add(ExchangeRate(fx_from="USD", fx_to="EUR", year=1999, rate=2))
        session.add(ExchangeRate(fx_from="EUR", fx_to="BEF", year=1999, rate=3))
        session.flush()
        staged = [{"fx_from": "USD", "fx_to": "BEF", "year": 1999, "rate": 7}]
        rep = check_fx_chain_closure(session, "x.md", staged)
        assert rep.blocked
        assert any(p["rule"] == "H3-汇率闭合" for p in rep.problems)

    def test_consistent_chain_clean(self, session):
        session.add(ExchangeRate(fx_from="USD", fx_to="EUR", year=1999, rate=2))
        session.add(ExchangeRate(fx_from="EUR", fx_to="BEF", year=1999, rate=3))
        session.flush()
        staged = [{"fx_from": "USD", "fx_to": "BEF", "year": 1999, "rate": 6}]
        rep = check_fx_chain_closure(session, "x.md", staged)
        assert rep.problems == []

    def test_staged_legs_participate(self, session):
        """两跳腿来自同一批暂存记录也能闭合校验。"""
        staged = [
            {"fx_from": "USD", "fx_to": "EUR", "year": 2000, "rate": 2},
            {"fx_from": "EUR", "fx_to": "BEF", "year": 2000, "rate": 3},
            {"fx_from": "USD", "fx_to": "BEF", "year": 2000, "rate": 5.5},  # 链式6 ≠ 5.5
        ]
        rep = check_fx_chain_closure(session, "x.md", staged)
        assert rep.blocked


class TestSeveritySplit:
    def test_h5_soft_h2_hard_coexist(self, session):
        """同文件：H5 软警告 + H2 硬冲突 → 整体 blocked，且 warnings 保留。"""
        h = Entity(entity_type="person", name="Henri Peeters")
        session.add(h)
        session.flush()
        from app.model import IncomeStream
        session.add(IncomeStream(entity_id=h.id, stream_type="shop", group_key="店",
                                 currency="BEF", year=2001, amount=100.0, label="既有"))
        session.flush()
        recs = [
            {"entity_name": "幽灵", "stream_type": "rent", "currency": "BEF",
             "year": 2005, "amount": 1.0},                       # H5 soft
            {"entity_name": "Henri Peeters", "stream_type": "shop",
             "currency": "BEF", "year": 2001, "amount": 555.0},  # H2 hard
        ]
        rep = check_income_stream_conflict(session, "混合.md", recs)
        assert rep.blocked
        assert any(p["rule"] == "H2-金额" for p in rep.problems)
        assert any(w["rule"] == "H5-引用" for w in rep.warnings)
