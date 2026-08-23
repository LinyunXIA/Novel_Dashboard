"""Unit tests for app/ingest/detect + parse_one SKIP 类别（issue #26 回归）。

覆盖：
- detect 显式映射 CPI工资.md → cpi_wage（不再落 unknown）
- detect 显式映射基准/公司/用工成本/ → SKIP_P1
- detect 显式映射设计文件/ → SKIP_DOC
- 顺序：更长前缀优先（CPI 工资.md 单独一个，不被 SKIP_P1 之类误吞）
- is_skip_category 判定
- parse_one：SKIP 类别 ok=True 不报 unknown error
- IngestReport.skipped 分组属性
"""
from __future__ import annotations

from pathlib import Path

from app.ingest.detect import detect, is_skip_category, scan_dir
from app.ingest.parse import IngestReport, ParseResult, parse_one


class TestDetectExplicitMappings:
    def test_cpi_wage_mapped(self):
        """issue #26 核心修复：CPI工资.md 不再落 unknown。"""
        d = detect("基准/CPI工资.md")
        assert d.category == "cpi_wage", f"应映射为 cpi_wage，实际 {d.category}"
        assert not d.phase2

    def test_company_labor_cost_skipped(self):
        """基准/公司/用工成本/ 显式 SKIP_P1（DESIGN §13 P1 范围）。"""
        d = detect("基准/公司/用工成本/比利时.md")
        assert d.category == "SKIP_P1"
        assert d.phase2 is False  # 不是 phase2 event，是 P1 跳过

    def test_design_docs_skipped(self):
        """设计文件/ 创作约束笔记 → SKIP_DOC（不入库）。"""
        d = detect("设计文件/学习.md")
        assert d.category == "SKIP_DOC"

    def test_nested_design_docs_skipped(self):
        """设计文件/ 子目录也走 SKIP_DOC。"""
        d = detect("设计文件/子目录/任何文件.md")
        assert d.category == "SKIP_DOC"


class TestIsSkipCategory:
    def test_skip_prefix_true(self):
        assert is_skip_category("SKIP_P1") is True
        assert is_skip_category("SKIP_DOC") is True

    def test_non_skip_false(self):
        assert is_skip_category("cpi_wage") is False
        assert is_skip_category("PHASE2_EVENT") is False
        assert is_skip_category("unknown") is False


class TestPrefixOrdering:
    def test_cpi_specific_before_company_prefix(self):
        """CPI 工资.md 是精确文件名前缀，应在「基准/」前匹配；这里只需验证 cpi_wage 命中。"""
        # 即便添加了 SKIP_P1 = "基准/公司/用工成本/" 前缀，CPI 文件路径不冲突
        d = detect("基准/CPI工资.md")
        assert d.category == "cpi_wage"

    def test_phase2_event_still_skipped(self):
        """回归：原有的 PHASE2_EVENT 映射不被破坏。"""
        d = detect("基准/事件/股票/腾讯.md")
        assert d.category == "PHASE2_EVENT"
        assert d.phase2 is True


class TestParseOneSkipCategory:
    def test_skip_p1_ok_no_error(self, tmp_path):
        """issue #26：SKIP 类别在 parse_one 显式跳过，ok=True 不报 unknown。"""
        p = tmp_path / "比利时.md"
        p.write_text("# 标题", encoding="utf-8")
        pr = parse_one("基准/公司/用工成本/比利时.md", p)
        assert pr.ok is True
        assert pr.error is None
        assert pr.category == "SKIP_P1"
        assert pr.records == []

    def test_skip_doc_ok_no_error(self, tmp_path):
        p = tmp_path / "学习.md"
        p.write_text("# 学习笔记", encoding="utf-8")
        pr = parse_one("设计文件/学习.md", p)
        assert pr.ok is True
        assert pr.category == "SKIP_DOC"


class TestIngestReportSkippedGroup:
    def test_skipped_filters_skip_categories(self):
        """IngestReport.skipped 单独分组 SKIP_*，不与 unknown 报错混淆。"""
        report = IngestReport()
        report.results.append(ParseResult(file="基准/公司/用工成本/X.md",
                                           category="SKIP_P1", ok=True))
        report.results.append(ParseResult(file="设计文件/学习.md",
                                           category="SKIP_DOC", ok=True))
        report.results.append(ParseResult(file="基准/CPI工资.md",
                                           category="cpi_wage", ok=True))
        report.results.append(ParseResult(file="未识别.md",
                                           category="unknown", ok=False,
                                           error="解析器未实现（unknown）"))
        skipped = report.skipped
        assert len(skipped) == 2
        assert all(r.category.startswith("SKIP_") for r in skipped)
        assert len(report.failed) == 1
        assert report.failed[0].category == "unknown"