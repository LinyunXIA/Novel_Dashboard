"""Unit tests for app/ingest/parsers.parse_bank + writer._ledger_kind_from_reason（issue #9 回归）。

覆盖：
- 节标题含「祖父」→ holder 解析
- 文件名（path.stem）兜底解析
- 多币种节（每个 segment 独立 currency/holder）
- kind 推断（inflow=投资收入 → investment_income；划拨 → income；支出 → expense）
- 缺 entity/缺 currency 的 segment 跳过
"""
from __future__ import annotations

from pathlib import Path

from app.ingest.parsers import parse_bank, _extract_holder_from_title, _extract_bank_name_from_header
from app.ingest.writer import _ledger_kind_from_reason


def _write(tmp: Path, name: str, content: str) -> Path:
    p = tmp / name
    p.write_text(content, encoding="utf-8")
    return p


class TestExtractHolderFromTitle:
    """返回规范 entity.name（TITLE_ENTITY 映射后），确保 account 唯一键一致。"""

    def test_exact_match(self):
        # 祖父 → 规范名 Henri Peeters
        assert _extract_holder_from_title("祖父") == "Henri Peeters"

    def test_substring_match(self):
        # 「祖父Henri Peeters注入」：长度倒序先命中 Henri Peeters（更长 key）
        assert _extract_holder_from_title("祖父Henri Peeters注入") == "Henri Peeters"

    def test_henri_peeters(self):
        assert _extract_holder_from_title("Henri Peeters 注入") == "Henri Peeters"

    def test_longer_key_wins(self):
        # 「外祖父」不能误吞「祖父」（外祖父更长 → 先命中）
        assert _extract_holder_from_title("外祖父 投资") == "Frederik van Oranje"

    def test_fallback_normalized(self):
        # fallback 也会按 TITLE_ENTITY 规范
        assert _extract_holder_from_title("随机标题", fallback="祖父") == "Henri Peeters"

    def test_no_match_no_fallback(self):
        assert _extract_holder_from_title("随机标题") is None


class TestExtractBankNameFromHeader:
    def test_basic(self, tmp_path):
        p = _write(tmp_path, "x.md", "# 账户\n# 开户行：德意志银行\n\n内容")
        assert _extract_bank_name_from_header(p.read_text(encoding="utf-8").splitlines()) == "德意志银行"

    def test_no_header(self, tmp_path):
        p = _write(tmp_path, "x.md", "# 账户\n# 无开户行\n\n内容")
        assert _extract_bank_name_from_header(p.read_text(encoding="utf-8").splitlines()) is None


class TestParseBankHolderResolution:
    """issue #9 修复重点：补持有人解析（标题 → 文件名兜底）。"""

    def test_holder_from_section_title(self, tmp_path):
        p = _write(tmp_path, "祖父.md", (
            "## 一、比利时法郎 BEF（祖父Henri Peeters注入）\n\n"
            "| 日期 | 理由 | 收入 | 支出 | 余额 | 备注 |\n"
            "|------|------|------|------|------|------|\n"
            "| 1982-01-01 | 祖父现金投资划拨 | 348.03 | | 348.03 | |\n"
        ))
        segs = parse_bank(p)
        assert len(segs) == 1
        # 祖父 → 规范名 Henri Peeters
        assert segs[0]["holder"] == "Henri Peeters"
        assert segs[0]["currency"] == "BEF"
        assert len(segs[0]["rows"]) == 1

    def test_holder_from_file_stem_fallback(self, tmp_path):
        # 节标题不含持有人，但 path.stem = "外祖父" → 通过 holders.py 兜底（规范化为 Frederik van Oranje）
        p = _write(tmp_path, "外祖父.md", (
            "## 一、荷兰盾 NLG\n\n"
            "| 日期 | 理由 | 收入 | 支出 | 余额 | 备注 |\n"
            "|------|------|------|------|------|------|\n"
            "| 1980-01-01 | 投资划拨 | 100 | | | |\n"
        ))
        segs = parse_bank(p)
        assert segs[0]["holder"] == "Frederik van Oranje"
        assert segs[0]["currency"] == "NLG"

    def test_multiple_segments(self, tmp_path):
        p = _write(tmp_path, "祖父.md", (
            "## 一、比利时法郎 BEF（祖父）\n\n"
            "| 日期 | 理由 | 收入 | 支出 | 余额 | 备注 |\n"
            "|------|------|------|------|------|------|\n"
            "| 1982-01-01 | 划拨 | 100 | | 100 | |\n"
            "\n"
            "## 二、卢森堡法郎 LUF（祖父）\n\n"
            "| 日期 | 理由 | 收入 | 支出 | 余额 | 备注 |\n"
            "|------|------|------|------|------|------|\n"
            "| 1982-01-01 | 划拨 | 50 | | 50 | |\n"
        ))
        segs = parse_bank(p)
        assert len(segs) == 2
        assert segs[0]["currency"] == "BEF"
        assert segs[1]["currency"] == "LUF"
        # 全部规范化为 Henri Peeters
        assert all(s["holder"] == "Henri Peeters" for s in segs)

    def test_bank_name_from_header(self, tmp_path):
        p = _write(tmp_path, "祖父.md", (
            "# 开户行：德意志银行\n\n"
            "## 一、比利时法郎 BEF（祖父）\n\n"
            "| 日期 | 理由 | 收入 | 支出 | 余额 | 备注 |\n"
            "|------|------|------|------|------|------|\n"
            "| 1982-01-01 | 划拨 | 100 | | 100 | |\n"
        ))
        segs = parse_bank(p)
        assert segs[0]["bank"] == "德意志银行"

    def test_no_holder_no_currency_segment_kept_but_holder_none(self, tmp_path):
        p = _write(tmp_path, "随机.md", (
            "## 一、某节标题\n\n"
            "| 日期 | 理由 | 收入 | 支出 | 余额 | 备注 |\n"
            "|------|------|------|------|------|------|\n"
            "| 1982-01-01 | 划拨 | 100 | | 100 | |\n"
        ))
        segs = parse_bank(p)
        # holder 解析失败 + 无兜底 → None（writer 会跳）
        assert segs[0]["holder"] is None


class TestLedgerKindFromReason:
    """issue #9：从「理由」推断 ledger.kind。"""

    def test_inflow_default(self):
        assert _ledger_kind_from_reason("", is_inflow=True) == "income"

    def test_outflow_default(self):
        assert _ledger_kind_from_reason("", is_inflow=False) == "expense"

    def test_securities_income(self):
        assert _ledger_kind_from_reason("证券收入×12月", is_inflow=True) == "investment_income"

    def test_leveraged_investment(self):
        assert _ledger_kind_from_reason("杠杆投资收益R5", is_inflow=True) == "investment_income"

    def test_capital_injection(self):
        assert _ledger_kind_from_reason("祖父现金投资划拨", is_inflow=True) == "income"

    def test_nlg_transfer(self):
        # 「资金转入」不在 income 子串里 → 走默认 income
        assert _ledger_kind_from_reason("NLG资金转入", is_inflow=True) == "income"

    def test_operating_expense(self):
        assert _ledger_kind_from_reason("运营支出（用工+外包服务）", is_inflow=False) == "expense"

    def test_property_income(self):
        assert _ledger_kind_from_reason("温泉庄园净值×12月", is_inflow=True) == "investment_income"