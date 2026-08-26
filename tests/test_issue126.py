"""issue #126 回归：search 数值铁律——确定性回填 + answer 数字后置校验。"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import BigInteger, create_engine, Integer
from sqlalchemy.orm import sessionmaker

import app.search.search as S
from app.db import Base
from app.model import Snapshot


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


class TestNumericGuard:
    def test_known_numbers_pass(self):
        ans = "1990 年家族总资产为 1,234.50 USD。"
        out = S.numeric_guard(ans, allowed_texts=["1990 年家族总资产（快照）= 1,234.50 USD"])
        assert out == ans

    def test_unknown_number_sentence_dropped(self):
        ans = "1990 年总资产约 1234.50 USD。另外据说有 999999 美元现金。"
        out = S.numeric_guard(ans, allowed_texts=["1990 年总资产 1234.50 USD"])
        assert "999999" not in out
        assert "1234.50" in out

    def test_all_unknown_falls_back(self):
        out = S.numeric_guard("大约有 7777 万。", allowed_texts=["无关文本"])
        assert out == "资料未提供相关确定性数值。"

    def test_non_numeric_answer_untouched(self):
        assert S.numeric_guard("资料未提供。", allowed_texts=[]) == "资料未提供。"


class TestBackfill:
    def test_no_intent_no_backfill(self, session):
        session.add(Snapshot(as_of_year=1990, as_of_date=None, scope="family:total",
                             value=Decimal("123456789"), currency="USD"))
        session.commit()
        assert S._backfill_wealth(session, "1990 年发生了什么") == []

    def test_intent_plus_year_returns_line(self, session):
        session.add(Snapshot(as_of_year=1990, as_of_date=None, scope="family:total",
                             value=Decimal("332276960"), currency="USD"))
        session.commit()
        lines = S._backfill_wealth(session, "1990 年家族总资产是多少")
        assert len(lines) == 1
        assert "332,276,960" in lines[0] and "3.32 亿" in lines[0]

    def test_missing_year_snapshot_skipped(self, session):
        assert S._backfill_wealth(session, "1991 年家族财富") == []


class TestSearchIntegration:
    def test_llm_fabricated_number_is_stripped(self, session, monkeypatch):
        monkeypatch.setattr(S, "embed", lambda qs, client=None: [[0.0] * 4])
        monkeypatch.setattr(S, "retrieve", lambda db, qv, k: [
            {"content": "1990 年家族合计 100 USD", "source_table": "snapshot",
             "source_row_id": 1}])
        monkeypatch.setattr(S, "chat",
                            lambda sys_, user, client=None: "1990 年家族总资产为 100 USD。\n另有来源不明的 55555 美元。")
        out = S.search(session, "1990 年家族总资产多少", client={})
        assert "55555" not in out["answer"]
        assert "100 USD" in out["answer"]
