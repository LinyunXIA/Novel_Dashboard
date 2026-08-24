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
