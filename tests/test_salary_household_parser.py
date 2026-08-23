"""Unit tests for app/ingest/parsers.parse_salary / parse_household_expense（issue #24 回归）。

覆盖：
- 备注列含数字时不被误采为税后值（关键修复点）
- 币种识别走 detect_currency，兼容「BEF（法郎）」等带后缀
- 表头缺失 → warning + 空 records（进 ingest_report）
- 金额解析失败 → warning 跳过该行
- household_expense 同等修复（不再硬取 cells[-1]、硬编码 BEF）
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.ingest.parsers import parse_household_expense, parse_salary


def _write(tmp: Path, name: str, content: str) -> Path:
    p = tmp / name
    p.write_text(content, encoding="utf-8")
    return p


class TestParseSalaryHeaderBased:
    def test_basic_after_tax_column(self, tmp_path):
        """正常场景：表头含「年份」「税后月薪」「BEF」，按列取金额。"""
        p = _write(tmp_path, "养父薪资.md", (
            "| 年份 | 涨薪 5% | 税后月薪 | BEF |\n"
            "|------|---------|----------|-----|\n"
            "| 1990 | 调薪 5% | 300000   | BEF |\n"
            "| 1991 | 调薪 5% | 315000   | BEF |\n"
        ))
        recs, warns = parse_salary(p)
        assert len(recs) == 2
        assert warns == []
        assert recs[0] == {"holder": "养父", "year": 1990, "currency": "BEF", "after_tax": 300000.0}
        assert recs[1]["after_tax"] == 315000.0

    def test_currency_with_chinese_suffix(self, tmp_path):
        """issue #24 修复点：币种列含「BEF（法郎）」等后缀时仍能被识别。"""
        p = _write(tmp_path, "养父薪资.md", (
            "| 年份 | 税后月薪 | 币种 |\n"
            "|------|----------|------|\n"
            "| 1995 | 350000   | BEF（法郎） |\n"
            "| 1996 | 360000   | NLG（荷兰盾） |\n"
        ))
        recs, warns = parse_salary(p)
        assert warns == []
        assert recs[0]["currency"] == "BEF"
        assert recs[1]["currency"] == "NLG"

    def test_note_column_with_numbers_not_misread(self, tmp_path):
        """issue #24 关键修复点：备注列含「1990」「5%」时不会被错采为税后值。

        此前「最后一个数字」逻辑会把「1990」（年份在备注列再次出现）误读。
        """
        p = _write(tmp_path, "养母薪资.md", (
            "| 年份 | 税后月薪 | 备注 |\n"
            "|------|----------|------|\n"
            "| 1990 | 250000   | 含 1990 年度奖金 5% 调整 |\n"
            "| 1991 | 260000   | 调薪 5% |\n"
        ))
        recs, warns = parse_salary(p)
        assert len(recs) == 2
        # 关键：after_tax 必须是 250000/260000，不是备注里的 1990 或 5
        assert recs[0]["after_tax"] == 250000.0
        assert recs[1]["after_tax"] == 260000.0
        assert warns == []

    def test_missing_header_warns_and_returns_empty(self, tmp_path):
        """issue #24 修复点：找不到「税后」表头 → warning，不静默错采。"""
        p = _write(tmp_path, "养父薪资.md", (
            "| 年份 | 月薪 |\n"
            "|------|------|\n"
            "| 1990 | 300000 |\n"
        ))
        recs, warns = parse_salary(p)
        assert recs == []
        assert len(warns) == 1
        assert "税后" in warns[0]

    def test_bad_amount_warns_per_row(self, tmp_path):
        """金额列无法解析时 warning 跳过该行（不进 ingest_report 静默丢）。"""
        p = _write(tmp_path, "养父薪资.md", (
            "| 年份 | 税后月薪 | BEF |\n"
            "|------|----------|-----|\n"
            "| 1990 | 300000   | BEF |\n"
            "| 1991 | n/a      | BEF |\n"
            "| 1992 | 320000   | BEF |\n"
        ))
        recs, warns = parse_salary(p)
        assert len(recs) == 2
        assert recs[0]["year"] == 1990
        assert recs[1]["year"] == 1992
        assert len(warns) == 1
        assert "1991" in warns[0]


class TestParseHouseholdExpenseHeaderBased:
    def test_basic_total_expense_column(self, tmp_path):
        """正常场景：表头含「年度总支出」+「BEF」。"""
        p = _write(tmp_path, "1974-2001家庭支出.md", (
            "| 年份 | 通胀系数 | 年度总支出 | BEF |\n"
            "|------|----------|------------|-----|\n"
            "| 1990 | 1.05     | 1500000    | BEF |\n"
            "| 1991 | 1.04     | 1560000    | BEF |\n"
        ))
        recs, warns = parse_household_expense(p)
        assert warns == []
        assert recs[0] == {"holder": "Henri Peeters", "year": 1990,
                           "amount": 1500000.0, "currency": "BEF"}
        assert recs[1]["amount"] == 1560000.0

    def test_total_in_last_column_not_misread(self, tmp_path):
        """issue #24 修复点：末列是「备注」而非「总支出」时不被错采。

        此前 cells[-1] 硬取最右列，错采备注。
        """
        p = _write(tmp_path, "家庭支出.md", (
            "| 年份 | 年度总支出 | 备注 |\n"
            "|------|------------|------|\n"
            "| 1990 | 1500000    | 含 1990 年度特殊支出 |\n"
        ))
        recs, warns = parse_household_expense(p)
        assert len(recs) == 1
        assert recs[0]["amount"] == 1500000.0
        assert warns == []

    def test_currency_column_recognized(self, tmp_path):
        """issue #24 修复点：币种列含「EUR」时不再硬编码 BEF。"""
        p = _write(tmp_path, "家庭支出.md", (
            "| 年份 | 年度总支出 | 币种 |\n"
            "|------|------------|------|\n"
            "| 2003 | 50000      | EUR |\n"
        ))
        recs, warns = parse_household_expense(p)
        assert warns == []
        assert recs[0]["currency"] == "EUR"

    def test_missing_header_warns(self, tmp_path):
        """issue #24 修复点：找不到「总支出」表头 → warning。"""
        p = _write(tmp_path, "家庭支出.md", (
            "| 年份 | 月支出 |\n"
            "|------|--------|\n"
            "| 1990 | 125000 |\n"
        ))
        recs, warns = parse_household_expense(p)
        assert recs == []
        assert len(warns) == 1
        assert "总支出" in warns[0]