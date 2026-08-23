"""Unit tests for app/ingest/parsers.parse_income_security（issue #11 回归）。

覆盖：
- 面值含小数点（如 4,047.30 BEF）能被正确解析
- 每券记录字段完整
- country 节存在但无任何 `### N.` 行 → warning 进 ingest_report
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.ingest.parsers import parse_income_security


def _write(tmp: Path, name: str, content: str) -> Path:
    p = tmp / name
    p.write_text(content, encoding="utf-8")
    return p


class TestParseIncomeSecurityFaceValue:
    def test_integer_face_value(self, tmp_path):
        # issue #11 之前 OK 的情况仍要保留
        p = _write(tmp_path, "祖产股票债券收益.md", (
            "## 比利时债券\n\n"
            "### 1. 比利时国债A（面值 1,000 BEF，固定票息 4.5%)\n"
        ))
        recs, warns = parse_income_security(p)
        assert len(recs) == 1
        assert recs[0]["face_value"] == 1000.0
        assert recs[0]["currency"] == "BEF"
        assert recs[0]["rate_pct"] == 4.5
        assert warns == []

    def test_decimal_face_value(self, tmp_path):
        """issue #11 修复重点：面值 4,047.30 BEF 此前因正则不含小数点而整条丢失。"""
        p = _write(tmp_path, "祖产股票债券收益.md", (
            "## 比利时债券\n\n"
            "### 1. 比利时国债A（面值 4,047.30 BEF，固定票息 4.5%)\n"
        ))
        recs, warns = parse_income_security(p)
        assert len(recs) == 1, f"面值带小数必须命中；recs={recs}, warns={warns}"
        assert recs[0]["face_value"] == 4047.30
        assert recs[0]["currency"] == "BEF"

    def test_decimal_face_value_multiple_bonds(self, tmp_path):
        p = _write(tmp_path, "祖产股票债券收益.md", (
            "## 荷兰债券\n\n"
            "### 1. 荷兰国债A（面值 1,234.56 NLG，固定票息 3.5%)\n"
            "### 2. 荷兰国债B（面值 7,890.12 NLG，固定票息 4.0%)\n"
        ))
        recs, warns = parse_income_security(p)
        assert len(recs) == 2
        assert recs[0]["face_value"] == 1234.56
        assert recs[1]["face_value"] == 7890.12
        assert warns == []


class TestParseIncomeSecurityWarnings:
    """issue #11：country 已设但无任何记录 → warning 进 ingest_report。"""

    def test_country_section_with_no_records_warns(self, tmp_path):
        p = _write(tmp_path, "祖产股票债券收益.md", (
            "## 比利时债券\n\n"
            "（这一节没有任何 ### N. 行）\n"
            "\n"
            "## 荷兰债券\n\n"
            "### 1. 荷兰国债A（面值 1,000 NLG，固定票息 4.0%)\n"
        ))
        recs, warns = parse_income_security(p)
        assert len(recs) == 1                           # 荷兰仍正常产出
        assert any("比利时" in w for w in warns), warns

    def test_country_section_with_records_no_warn(self, tmp_path):
        p = _write(tmp_path, "祖产股票债券收益.md", (
            "## 丹麦债券\n\n"
            "### 1. 丹麦国债（面值 1,000.00 DKK，固定票息 3.5%)\n"
        ))
        recs, warns = parse_income_security(p)
        assert len(recs) == 1
        assert warns == []


class TestParseIncomeSecurityHolder:
    """country → holder 映射（铁律）。"""

    @pytest.mark.parametrize("country,expected_holder,expected_cur", [
        ("荷兰", "养外祖父", "NLG"),
        ("丹麦", "养外祖母", "DKK"),
        ("瑞典", "养祖母", "SEK"),
        ("比利时", "养祖父", "BEF"),
        ("卢森堡", "养祖父", "LUF"),
    ])
    def test_country_holder_mapping(self, tmp_path, country, expected_holder, expected_cur):
        p = _write(tmp_path, "祖产股票债券收益.md", (
            f"## {country}债券\n\n"
            f"### 1. 测试券（面值 1,000 {expected_cur}，固定票息 4.0%)\n"
        ))
        recs, _ = parse_income_security(p)
        assert len(recs) == 1
        assert recs[0]["holder"] == expected_holder
        assert recs[0]["currency"] == expected_cur