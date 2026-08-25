"""issue #69：收益展开因子配置——分段边界与源文件终值系数的一致性。

issue #114 口径定案 A「文件终值权威」：调价在每年年初（含起租年 1974）、
年末结算入账 → factor(1984)=1.07¹¹≈2.1049、factor(2007)=5.2100，
与 经营性房产收益.md / 惠民租房.md 的示例金额逐字一致。
"""
from __future__ import annotations

from app.core.factors import (
    SECURITY_DEFAULT_YEARS, compound_factor, property_factor, rent_factor,
)


class TestPropertyFactor:
    def test_base_year_applies_first_raise(self):
        # 起租年当年即计首次涨幅（年初调价、年末结算）
        assert abs(property_factor(1974) - 1.07) < 1e-9
        assert property_factor(1950) == 1.0

    def test_segment_boundaries_match_source_file(self):
        # 源文件：① 1974–1984（11年）1.07¹¹≈2.1049
        assert abs(property_factor(1984) - 1.07 ** 11) < 1e-9
        assert abs(property_factor(1984) - 2.1049) < 1e-3
        # ② 1985 起 +3.5%
        assert abs(property_factor(1985) - 1.07 ** 11 * 1.035) < 1e-9
        # ③ 1974→2007 累计总系数 ≈5.2100（源文件 L13/L74/L116）
        f2007 = 1.07 ** 11 * 1.035 ** 15 * 1.05 ** 8
        assert abs(property_factor(2007) - f2007) < 1e-9
        assert abs(f2007 - 5.2100) < 1e-3
        # 2008 起 +3%
        assert abs(property_factor(2008) - f2007 * 1.03) < 1e-9

    def test_open_ended_tail(self):
        f2022 = 1.07 ** 11 * 1.035 ** 15 * 1.05 ** 8 * 1.03 ** 9 * 1.028 ** 6
        assert abs(property_factor(2022) - f2022) < 1e-9
        assert abs(property_factor(2030) - f2022 * 1.015 ** 8) < 1e-6


class TestRentFactor:
    def test_boundaries_match_source_file(self):
        assert abs(rent_factor(1974) - 1.07) < 1e-9
        # 惠民租房.md §三：1974–1984 终值系数 1.07¹¹≈2.1049；示例 18000×2.1049=37888.2
        assert abs(rent_factor(1984) - 1.07 ** 11) < 1e-9
        assert abs(rent_factor(1999) - 1.07 ** 11 * 1.035 ** 15) < 1e-9
        # 2000 起 +5%（开放末端）
        assert abs(rent_factor(2007) - 1.07 ** 11 * 1.035 ** 15 * 1.05 ** 8) < 1e-9


class TestSecurityWindow:
    def test_default_window(self):
        """展开窗口默认 1947–2025，参数化后行为不变。"""
        assert SECURITY_DEFAULT_YEARS == (1947, 2025)

    def test_compound_generic(self):
        segs = ((None, 1.02),)
        # 基桩年当年起即应用段率（与 A 口径一致）
        assert abs(compound_factor(1990, segs, base_year=1990) - 1.02) < 1e-9
        assert abs(compound_factor(2000, segs, base_year=1990) - 1.02 ** 11) < 1e-9
        assert abs(compound_factor(1995, segs, base_year=1990) - 1.02 ** 6) < 1e-9
        assert compound_factor(1989, segs, base_year=1990) == 1.0
