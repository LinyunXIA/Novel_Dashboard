"""Unit tests for app/ingest/normalize（issue #18 parse_number 万/亿、#19 parse_date_cell 日期规则）。

覆盖：
- parse_number：千分位 / ≈ / 万 / 亿 乘基数 / 失败回 None
- parse_amount：单位剥离（含「万美金」长词优先）
- parse_date_cell：YYYY / YYYY-MM / YYYY-MM-DD / 中文分隔 / 年初·上中下旬 / 超规则回 None
"""
from __future__ import annotations

from datetime import date

from app.ingest.normalize import parse_number, parse_amount, parse_date_cell


class TestParseNumber:
    def test_thousands(self):
        assert parse_number("12,345") == 12345.0

    def test_approx(self):
        assert parse_number("≈4,047.30") == 4047.3

    def test_wan_units(self):
        # issue #18：万 乘基数（此前静默回 None）
        assert parse_number("1.2万") == 12000.0
        assert parse_number("1万") == 10000.0

    def test_yi_units(self):
        # issue #18：亿 乘基数
        assert parse_number("≈1.258亿") == 125800000.0
        assert parse_number("2亿") == 200000000.0

    def test_scale(self):
        assert parse_number("100", scale=10.0) == 1000.0

    def test_none_and_dash(self):
        assert parse_number(None) is None
        assert parse_number("-") is None
        assert parse_number("—") is None

    def test_invalid(self):
        assert parse_number("abc") is None
        assert parse_number("1.2万亿") is None  # 复合单位未支持 → None


class TestParseAmount:
    def test_strip_currency(self):
        assert parse_amount("1200USD") == 1200.0

    def test_wan_meijin_long_word(self):
        # issue #18：「万美金」整词优先剥离，不再残留「美金」
        assert parse_amount("1.2万美金") == 1.2

    def test_wan_only(self):
        assert parse_amount("1.2万") == 1.2


class TestParseDateCell:
    def test_iso_full(self):
        d, p = parse_date_cell("1992-12-31")
        assert (d, p) == (date(1992, 12, 31), "year-month-day")

    def test_slash_full(self):
        d, _p = parse_date_cell("1992/01/15")
        assert d == date(1992, 1, 15)

    def test_cn_full(self):
        d, p = parse_date_cell("1992年6月30日")
        assert (d, p) == (date(1992, 6, 30), "year-month-day")

    def test_year_month(self):
        # 仅年+月 → 该月月底（1992 闰年 → 2 月 29）
        d, p = parse_date_cell("1992-02")
        assert (d, p) == (date(1992, 2, 29), "year-month")

    def test_cn_year_month(self):
        d, p = parse_date_cell("1950年2月")
        assert (d, p) == (date(1950, 2, 28), "year-month")

    def test_early_mid_late(self):
        assert parse_date_cell("1992年2月上旬")[0] == date(1992, 2, 1)
        assert parse_date_cell("1992年2月中旬")[0] == date(1992, 2, 11)
        assert parse_date_cell("1992年2月下旬")[0] == date(1992, 2, 21)

    def test_beginning_of_year(self):
        d, p = parse_date_cell("1992年初")
        assert (d, p) == (date(1992, 1, 1), "year")

    def test_year_only(self):
        # 仅年 → 12-30（DESIGN §6.2 默认）
        d, p = parse_date_cell("1992")
        assert (d, p) == (date(1992, 12, 30), "year")

    def test_out_of_spec(self):
        assert parse_date_cell("约1992年") == (None, None)
        assert parse_date_cell("1992-13-01") == (None, None)
        assert parse_date_cell("1992-02-30") == (None, None)
        assert parse_date_cell("") == (None, None)