"""issue #69：收益展开因子配置——分段边界与旧硬编码实现的等价性。"""
from __future__ import annotations

from app.core.factors import (
    SECURITY_DEFAULT_YEARS, compound_factor, property_factor, rent_factor,
)


class TestPropertyFactor:
    def test_base_year_is_one(self):
        assert property_factor(1974) == 1.0
        assert property_factor(1950) == 1.0

    def test_segment_boundaries(self):
        assert abs(property_factor(1975) - 1.07) < 1e-9
        assert abs(property_factor(1984) - 1.07 ** 10) < 1e-9
        # 1985 起切换 +3.5%
        assert abs(property_factor(1985) - 1.07 ** 10 * 1.035) < 1e-9
        # 2008 起 +3%
        f2007 = 1.07 ** 10 * 1.035 ** 15 * 1.05 ** 8
        assert abs(property_factor(2007) - f2007) < 1e-9
        assert abs(property_factor(2008) - f2007 * 1.03) < 1e-9

    def test_open_ended_tail(self):
        f2022 = 1.07 ** 10 * 1.035 ** 15 * 1.05 ** 8 * 1.03 ** 9 * 1.028 ** 6
        assert abs(property_factor(2022) - f2022) < 1e-9
        assert abs(property_factor(2030) - f2022 * 1.015 ** 8) < 1e-6


class TestRentFactor:
    def test_boundaries(self):
        assert rent_factor(1974) == 1.0
        assert abs(rent_factor(1999) - 1.07 ** 10 * 1.035 ** 15) < 1e-9
        # 2000 起 +5%（开放末端）
        assert abs(rent_factor(2007) - 1.07 ** 10 * 1.035 ** 15 * 1.05 ** 8) < 1e-9


class TestSecurityWindow:
    def test_default_window(self):
        """展开窗口默认 1947–2025，参数化后行为不变。"""
        assert SECURITY_DEFAULT_YEARS == (1947, 2025)

    def test_compound_generic(self):
        segs = ((None, 1.02),)
        assert compound_factor(1990, segs, base_year=1990) == 1.0
        assert abs(compound_factor(2000, segs, base_year=1990) - 1.02 ** 10) < 1e-9
        assert abs(compound_factor(1995, segs, base_year=1990) - 1.02 ** 5) < 1e-9
