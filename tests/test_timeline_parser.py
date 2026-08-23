"""Unit tests for app/ingest/parsers.parse_timeline（issue #8 回归）。

覆盖：decade 标题、年份格式（YYYY / YYYY-MM / YYYY-MM-DD）、备注列、event_date 解析。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path


from app.ingest.parsers import parse_timeline


def _write(tmp: Path, name: str, content: str) -> Path:
    p = tmp / name
    p.write_text(content, encoding="utf-8")
    return p


class TestParseTimelineBasic:
    def test_year_only(self, tmp_path):
        p = _write(tmp_path, "时间线.md", (
            "## 1940s\n\n"
            "| 年份 | 事件 | 备注 |\n"
            "|------|------|------|\n"
            "| 1947 | 祖母去世 | 由祖父继承 |\n"
        ))
        recs, _w = parse_timeline(p)
        assert len(recs) == 1
        assert recs[0]["event_year"] == 1947
        assert recs[0]["title"] == "祖母去世"
        assert recs[0]["note"] == "由祖父继承"
        assert recs[0]["decade"] == "1940s"
        # issue #8：年份-only 默认 → 12-30（DESIGN §6.2 默认规则 F）
        assert recs[0]["event_date"] == date(1947, 12, 30)

    def test_iso_date(self, tmp_path):
        p = _write(tmp_path, "时间线.md", (
            "## 1970s\n\n"
            "| 年份 | 事件 | 备注 |\n"
            "|------|------|------|\n"
            "| 1974-01-01 | 主角出生 | - |\n"
        ))
        recs, _w = parse_timeline(p)
        assert len(recs) == 1
        assert recs[0]["event_year"] == 1974
        assert recs[0]["event_date"] == date(1974, 1, 1)


class TestParseTimelineMultiple:
    def test_multiple_decades(self, tmp_path):
        p = _write(tmp_path, "时间线.md", (
            "## 1980s\n\n"
            "| 年份 | 事件 | 备注 |\n"
            "|------|------|------|\n"
            "| 1985 | 事件A | - |\n"
            "\n"
            "## 1990s\n\n"
            "| 年份 | 事件 | 备注 |\n"
            "|------|------|------|\n"
            "| 1990 | 事件B | - |\n"
            "| 1992-09-16 | 黑色星期三 | 英镑脱钩ERM |\n"
        ))
        recs, _w = parse_timeline(p)
        assert len(recs) == 3
        assert [r["event_year"] for r in recs] == [1985, 1990, 1992]
        assert [r["decade"] for r in recs] == ["1980s", "1990s", "1990s"]
        assert recs[2]["event_date"] == date(1992, 9, 16)

    def test_header_row_skipped(self, tmp_path):
        p = _write(tmp_path, "时间线.md", (
            "## 2000s\n\n"
            "| 年份 | 事件 | 备注 |\n"
            "|------|------|------|\n"
            "| 2008 | 金融危机 | - |\n"
        ))
        recs, _w = parse_timeline(p)
        # 表头「年份」不应被当作记录（"年份" in cells[0] → 跳过）
        assert len(recs) == 1
        assert recs[0]["event_year"] == 2008

    def test_empty_note(self, tmp_path):
        p = _write(tmp_path, "时间线.md", (
            "## 1990s\n\n"
            "| 年份 | 事件 | 备注 |\n"
            "|------|------|------|\n"
            "| 1995 | 事件 |  |\n"
        ))
        recs, _w = parse_timeline(p)
        assert len(recs) == 1
        # 备注空白 → None 或空字符串（trim 后存）
        assert recs[0]["note"] in (None, "")

class TestParseTimelineDateRule:
    """issue #19：日期统一走 resolve_date 规则；超规则进 warnings 提示补 date_rule。"""

    def test_hinted_date(self, tmp_path):
        p = _write(tmp_path, "时间线.md", (
            "## 1990s\n\n"
            "| 年份 | 事件 | 备注 |\n"
            "|------|------|------|\n"
            "| 1992年2月上旬 | 某事 | - |\n"
        ))
        recs, _w = parse_timeline(p)
        # 上旬 → 2 月 1 日（DESIGN §6.2 默认规则 F）
        assert recs[0]["event_date"] == date(1992, 2, 1)

    def test_out_of_spec_warns(self, tmp_path):
        p = _write(tmp_path, "时间线.md", (
            "## 1990s\n\n"
            "| 年份 | 事件 | 备注 |\n"
            "|------|------|------|\n"
            "| 约1992年 | 某事 | - |\n"
        ))
        recs, warnings = parse_timeline(p)
        # 超规则 → 回退当年默认(12-30) + 进 ingest_report 提示补 date_rule
        assert recs[0]["event_date"] == date(1992, 12, 30)
        assert warnings, "超规则日期应产生 date_rule 提示"
        assert "date_rule" in warnings[0]
