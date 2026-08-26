"""issue #71：core/currency.usd_rate 权威折算实现（合并双份 _usd_rate 后的行为钉定）。"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.currency import usd_rate
from app.model import Base, ExchangeRate


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


def _fx(s, f, t, year, rate):
    s.add(ExchangeRate(fx_from=f, fx_to=t, year=year, rate=rate))


class TestUsdRate:
    def test_usd_identity(self, session):
        assert usd_rate(session, "USD", 1990) == Decimal(1)

    def test_forward_reciprocal(self, session):
        """USD→BEF 行 rate=1USD 兑 X → 取倒数。"""
        _fx(session, "USD", "BEF", 1990, 32)
        assert usd_rate(session, "BEF", 1990) == Decimal(1) / Decimal(32)

    def test_constant_fallback(self, session):
        """该年无行 → 回退基准常量（year IS NULL）。"""
        _fx(session, "USD", "BEF", None, 40.3399)
        assert usd_rate(session, "BEF", 1990) == Decimal(1) / Decimal("40.3399")

    def test_specific_year_beats_constant(self, session):
        _fx(session, "USD", "BEF", None, 40)
        _fx(session, "USD", "BEF", 1991, 38)
        assert usd_rate(session, "BEF", 1991) == Decimal(1) / Decimal(38)

    def test_reverse_direct(self, session):
        _fx(session, "BEF", "USD", None, Decimal("0.03"))
        assert usd_rate(session, "BEF", 1990) == Decimal("0.03")

    def test_missing_returns_none_never_one(self, session):
        """数值纪律：缺汇率返回 None，绝不静默 fallback 1.0。"""
        assert usd_rate(session, "SEK", 1990) is None


class TestChainedUsdRate:
    """issue #115：直连缺失时经 EUR 枢纽两跳连乘；宁缺勿错不变。"""

    def test_chained_via_eur_reciprocal_legs(self, session):
        # 库里只有 EUR→SEK（1EUR=9.2420SEK）与 USD→EUR 方向的行 → 倒数成链
        _fx(session, "EUR", "SEK", None, "9.2420")
        _fx(session, "USD", "EUR", 1995, "1.25")   # 1USD=1.25EUR → 1EUR=0.8USD
        r = usd_rate(session, "SEK", 1995)
        assert r == (Decimal(1) / Decimal("9.2420") * Decimal("0.8")).quantize(Decimal("0.000001"))

    def test_chained_via_eur_direct_legs(self, session):
        _fx(session, "SEK", "EUR", None, "0.1082")
        _fx(session, "EUR", "USD", 1995, "0.8")
        assert usd_rate(session, "SEK", 1995) == Decimal("0.086560")

    def test_chained_missing_leg_returns_none(self, session):
        """任一腿缺失仍返回 None，绝不 fallback。"""
        _fx(session, "EUR", "SEK", None, "9.2420")   # 有 X→EUR 无 EUR→USD
        assert usd_rate(session, "SEK", 1995) is None

    def test_direct_still_beats_chained(self, session):
        _fx(session, "EUR", "SEK", None, "9.2420")
        _fx(session, "USD", "EUR", 1995, "1.25")
        _fx(session, "USD", "SEK", 1995, "7.00")
        assert usd_rate(session, "SEK", 1995) == Decimal(1) / Decimal("7.00")
