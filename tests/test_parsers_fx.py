"""Unit tests for app/ingest/parsers.parse_fx（issue #25 回归）。

覆盖：
- 跨年文件 `1999-2002.md`：行内无年份 token → 文件级 fallback（首个 4 位 token=1999）
- 行内显式年份（节标题型 1999/2000/2001/2002 分节）按行各自归年
- 全无年份（仅 `1EUR=40.3399BEF`） → year=NULL（基准常量）
- TSV 格式表行也走按行定年逻辑
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.ingest.parsers import parse_fx


def _write(tmp: Path, name: str, content: str) -> Path:
    p = tmp / name
    p.write_text(content, encoding="utf-8")
    return p


class TestParseFxLineYear:
    def test_multiyear_file_fallback_to_filename(self, tmp_path):
        """issue #25 核心场景：1999-2002.md 行内无年份 token，应回退到文件级。

        现状：整文件共用 cur_year（=1999）。修复后行为等价——fallback 仍是 1999，
        但语义从"单一来源"改为"行内优先 → 文件级 fallback"。
        """
        p = _write(tmp_path, "1999-2002.md", (
            "# 1999.1-2002.12 EUR对BEF,LUF,NLG汇率\n"
            "1EUR=40.3399BEF\n"
            "1EUR=40.3399LUF\n"
            "1EUR=2.20371NLG\n"
        ))
        recs = parse_fx(p)
        assert len(recs) == 3
        # 文件级 fallback 应归到 1999（从首个 4 位 token 或文件名）
        years = {r["year"] for r in recs}
        assert years == {1999}, f"预期全部回退到 1999，实际 {years}"

    def test_explicit_year_per_line(self, tmp_path):
        """行内含 19xx/20xx token 时，每行独立定年（issue #25 主要修复点）。"""
        p = _write(tmp_path, "汇率表.md", (
            "1999\n"
            "1EUR=40.3399BEF\n"
            "2000\n"
            "1EUR=40.3399BEF\n"
            "2001\n"
            "1EUR=40.3399BEF\n"
            "2002\n"
            "1EUR=40.3399BEF\n"
        ))
        recs = parse_fx(p)
        assert len(recs) == 4
        # 每行应取其节标题年份，不再全部归到 1999
        assert [r["year"] for r in recs] == [1999, 2000, 2001, 2002], \
            f"按行定年失败：{[(r['fx_to'], r['year']) for r in recs]}"

    def test_no_year_anywhere_yields_null(self, tmp_path):
        """全无年份（无文件名 token 也无行内 token）→ year=NULL（基准常量）。"""
        p = _write(tmp_path, "基准常量.md", (
            "# 基准汇率（无年份）\n"
            "1EUR=40.3399BEF\n"
        ))
        recs = parse_fx(p)
        assert len(recs) == 1
        assert recs[0]["year"] is None, \
            f"全无年份必须落 NULL，实际 {recs[0]['year']}"

    def test_filename_year_takes_effect(self, tmp_path):
        """文件名含 4 位年份 + 行内无 token → 文件级 fallback 命中文件名。"""
        p = _write(tmp_path, "1995.md", (
            "Belgian Franc  BEF  32.14\n"
            "Dutch Guilder  NLG  1.61\n"
        ))
        recs = parse_fx(p)
        assert len(recs) == 2
        assert all(r["year"] == 1995 for r in recs)
        assert {r["fx_to"] for r in recs} == {"BEF", "NLG"}

    def test_line_year_overrides_filename(self, tmp_path):
        """行内年份 token 优先于文件名（issue #25 语义核心）。"""
        p = _write(tmp_path, "1995.md", (
            "2000\n"
            "Belgian Franc  BEF  32.14\n"
        ))
        recs = parse_fx(p)
        assert len(recs) == 1
        # 行内 2000 优先于文件名 1995
        assert recs[0]["year"] == 2000