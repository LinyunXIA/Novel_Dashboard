"""issue #211：基本收入.md parser（parse_basic_income）+ detect 路由单测。

覆盖：
- 人物节 holder 映射（养外祖父→Frederik van Oranje / Henri Peeters / 养外祖母 / 养祖母）；
  `## 五、汇总` 节 holder=None，表格全部跳过
- 年份段展开：en-dash `–` 与 hyphen `-` 均可，段内每年同值
- 币种：`NLG/年` 后缀识别、单码格、`BEF/LUF` 双币格按列拆分（祖父 BEF / 先祖 LUF）、
  双币表内 EUR 行全部 EUR
- 0 值跳过（惠民租房 2008 起记 0 → 无 rent 行，issue #28）
- 商业表（shop）时段展开、stream_type/group_key 归类
- `合计` 列分量核对不符 → warning；币种无法识别 → warning 且该行跳过
- detect：基本收入.md → basic_income；旧 4 文件 → SKIP_SUPERSEDED（parse_one 不报错）
"""
from __future__ import annotations

import pytest

from app.ingest.detect import detect, is_skip_category
from app.ingest.parsers import parse_basic_income
from app.ingest.parse import parse_one


_MINI = """# 五人初始资产逐年收益明细（测试）

## 一、养外祖父 Frederik van Oranje（荷兰）

### 1.1 股票债券逐年收益

| 年份 | 债券收益 | 股票收益 | 合计 | 货币 |
|---:|---:|---:|---:|---|
| 1950–1955 | 100 | 200 | 300 | NLG/年 |
| 2002-2003 | 50 | 60 | 110 | EUR/年 |
| 1980 | 5 | 6 | 11 | XYZ/年 |

### 1.2 房产逐年收益

| 年份 | 惠民租房 | 经营性房产 | 合计 | 货币 |
|---:|---:|---:|---:|---|
| 1974 | 10 | 1,000 | 1,010 | NLG |
| 2008 | 0 | 500 | 500 | EUR |

## 四、Henri Peeters（比利时，含先祖卢森堡资产）

### 4.2 房产逐年收益

| 年份 | 惠民(祖父) | 惠民(先祖) | 经营性(祖父) | 经营性(先祖) | 合计 | 货币 |
|---:|---:|---:|---:|---:|---:|---|
| 1974 | 18 | 142 | 288 | 3,168 | 3,616 | BEF/LUF |
| 2002 | 1 | 2 | 3 | 4 | 10 | EUR |

### 4.3 商业逐年收益

| 年份 | 税后落袋 | 货币 |
|---:|---:|---|
| 1947–1949 | 800 | BEF/年 |

## 五、全周期累计汇总

| 人物 | 股债累计 | 房产累计 | 商业累计 | 总计 |
|---|---:|---:|---:|---:|
| 养外祖父 | 999,999 NLG | 888,888 NLG | — | 1,888,887 NLG |
"""


@pytest.fixture()
def mini_file(tmp_path):
    p = tmp_path / "基本收入.md"
    p.write_text(_MINI, encoding="utf-8")
    return p


def _by(recs, **kw):
    return [r for r in recs if all(r.get(k) == v for k, v in kw.items())]


class TestHolderAndSections:
    def test_holder_mapping(self, mini_file):
        recs, _ = parse_basic_income(mini_file)
        holders = {r["holder"] for r in recs}
        assert holders == {"Frederik van Oranje", "Henri Peeters"}

    def test_summary_section_ignored(self, mini_file):
        """§5 汇总节（999,999/888,888）不得产出任何记录。"""
        recs, warns = parse_basic_income(mini_file)
        assert not _by(recs, amount=999999)
        assert not _by(recs, amount=888888)
        # 汇总表首列是「人物」而非年份，本就不会被当年份行解析
        assert recs, "明细表应正常产出记录"


class TestSegmentExpansion:
    def test_endash_range_expands_per_year(self, mini_file):
        recs, _ = parse_basic_income(mini_file)
        sec = _by(recs, holder="Frederik van Oranje", stream_type="security",
                  currency="NLG")
        assert len(sec) == 12          # 6 年（1950–1955）× 债券/股票 2 列
        assert {r["year"] for r in sec} == set(range(1950, 1956))
        assert {r["amount"] for r in _by(sec, group_key="祖产债券")} == {100}
        assert {r["amount"] for r in _by(sec, group_key="祖产股票")} == {200}

    def test_hyphen_range_also_expands(self, mini_file):
        recs, _ = parse_basic_income(mini_file)
        eur = _by(recs, stream_type="security", currency="EUR")
        assert len(eur) == 4           # 2002-2003（hyphen）× 2 列
        assert {r["year"] for r in eur} == {2002, 2003}

    def test_shop_range_expands(self, mini_file):
        recs, _ = parse_basic_income(mini_file)
        shop = _by(recs, stream_type="shop")
        assert len(shop) == 3          # 1947–1949
        assert {r["year"] for r in shop} == {1947, 1948, 1949}
        assert {r["currency"] for r in shop} == {"BEF"}
        assert {r["group_key"] for r in shop} == {"祖父开店"}
        assert all(r["holder"] == "Henri Peeters" for r in shop)


class TestCurrencySplit:
    def test_bef_luf_dual_cell_split_by_column(self, mini_file):
        """BEF/LUF 双币格：祖父两列 BEF、先祖两列 LUF（1:1 数值，币种分列）。"""
        recs, _ = parse_basic_income(mini_file)
        row74 = _by(recs, holder="Henri Peeters", year=1974)
        assert len(row74) == 4
        bef = _by(row74, currency="BEF")
        luf = _by(row74, currency="LUF")
        assert {r["group_key"] for r in bef} == {"惠民租房·祖父", "经营性房产·祖父"}
        assert {r["group_key"] for r in luf} == {"惠民租房·先祖", "经营性房产·先祖"}
        assert _by(bef, stream_type="rent")[0]["amount"] == 18
        assert _by(luf, stream_type="property")[0]["amount"] == 3168

    def test_eur_rows_in_split_table_all_eur(self, mini_file):
        recs, _ = parse_basic_income(mini_file)
        row02 = _by(recs, holder="Henri Peeters", year=2002)
        assert len(row02) == 4
        assert {r["currency"] for r in row02} == {"EUR"}

    def test_suffix_currency_recognized(self, mini_file):
        """`NLG/年`、`BEF/年`、`EUR/年` 后缀不影响币种识别。"""
        recs, warns = parse_basic_income(mini_file)
        assert _by(recs, currency="NLG")
        assert _by(recs, currency="BEF")
        assert _by(recs, currency="EUR")


class TestZeroSkipAndGroupKeys:
    def test_zero_rent_2008_no_row(self, mini_file):
        """惠民租房 2008 记 0 → 无 rent 行；经营性 500 正常入库。"""
        recs, _ = parse_basic_income(mini_file)
        rent08 = _by(recs, stream_type="rent", year=2008)
        assert rent08 == []
        prop08 = _by(recs, stream_type="property", year=2008)
        assert len(prop08) == 1 and prop08[0]["amount"] == 500

    def test_group_keys_and_labels(self, mini_file):
        recs, _ = parse_basic_income(mini_file)
        assert _by(recs, group_key="祖产债券")
        assert _by(recs, group_key="祖产股票")
        assert _by(recs, group_key="惠民租房")
        assert _by(recs, group_key="经营性房产")
        assert _by(recs, group_key="惠民租房·先祖")
        assert _by(recs, group_key="经营性房产·先祖")
        # label 带来源语义
        labels = {r["label"] for r in recs}
        assert "祖产股票债券 · 债券收益" in labels
        assert "祖父开店 · 合并税后落袋" in labels

    def test_source_line_carried(self, mini_file):
        recs, _ = parse_basic_income(mini_file)
        assert all(isinstance(r["source_line"], int) and r["source_line"] > 0
                   for r in recs)


class TestWarnings:
    def test_unknown_currency_warns_and_skips_row(self, mini_file):
        """币种格无法识别 → warning，且该行（1980 XYZ）不产记录。"""
        recs, warns = parse_basic_income(mini_file)
        assert _by(recs, year=1980) == []
        assert any("币种无法识别" in w and "XYZ" in w for w in warns)

    def test_total_mismatch_warns(self, tmp_path):
        p = tmp_path / "基本收入.md"
        p.write_text(
            "## 二、养外祖母（丹麦）\n\n"
            "| 年份 | 惠民租房 | 经营性房产 | 合计 | 货币 |\n"
            "|---:|---:|---:|---:|---|\n"
            "| 1974 | 10 | 1,000 | 9,999 | DKK |\n",
            encoding="utf-8")
        recs, warns = parse_basic_income(p)
        assert len(recs) == 2             # 分量行仍入库
        assert any("合计" in w and "9999" in w for w in warns)

    def test_unrecognized_header_warns(self, tmp_path):
        p = tmp_path / "基本收入.md"
        p.write_text(
            "## 三、养祖母（瑞典）\n\n"
            "| 年份 | 神秘收入 | 合计 | 货币 |\n|---:|---:|---:|---|\n"
            "| 1990 | 100 | 100 | SEK |\n",
            encoding="utf-8")
        recs, warns = parse_basic_income(p)
        assert recs == []
        assert any("表头未识别" in w for w in warns)


class TestDetectRouting:
    def test_basic_income_category(self):
        d = detect("基准/收益表/基本收入.md")
        assert d.category == "basic_income"
        assert not is_skip_category(d.category)

    @pytest.mark.parametrize("name", [
        "惠民租房.md", "经营性房产收益.md", "祖产股票债券收益.md", "祖父开店.md",
    ])
    def test_old_income_files_superseded(self, name):
        d = detect(f"基准/收益表/{name}")
        assert d.category == "SKIP_SUPERSEDED"
        assert is_skip_category(d.category)

    def test_parse_one_old_file_ok_no_records(self, tmp_path):
        """旧文件 parse_one 显式跳过：ok=True、无记录、不报 unknown。"""
        p = tmp_path / "惠民租房.md"
        p.write_text("# 旧惠民租房（存档）\n", encoding="utf-8")
        pr = parse_one("基准/收益表/惠民租房.md", p)
        assert pr.ok is True and pr.error is None
        assert pr.category == "SKIP_SUPERSEDED"
        assert pr.records == []
