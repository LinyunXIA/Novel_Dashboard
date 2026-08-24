"""用工成本基准解析器单测（API② · F-P1-10）。

覆盖已知坑：CPI 全角/半角双分隔符(含 TKY)、`*`/千分位剥离、比/卢 2002 双币拆行、
日本 CPI 基年 2015、税率北京第二张对比表排除（防重复键）。
"""
from __future__ import annotations

import pytest
from pathlib import Path

from app.ingest import labor_baseline as L


WAGE_8COL = (
    "| 年份 | 全行业人均名义年薪（基准） | 币种 | CPI(2013=100) | 投资/金融行业年薪 | 高科技/ICT行业年薪 | 企业年度税费总和 | 总用工成本（税前+税费） |\n"
    "| ---- | ---- | ---- | ---- | ---- | ---- | ---- | ---- |\n"
    "{rows}"
)


def test_wage_dual_currency_2002_splits( tmp_path):
    rows = (
        "| 1982 | 673730.00* | BEF | 35.05 | 1010595.00* | 943222.00* | 161695.00 | 835425.00 |\n"
        "| 2002 | 1560000.00 / 38672.00 | BEF / EUR | 61.44 | 2340000.00 / 58008.00 | 2184000.00 / 54141.00 | 374400.00 / 9282.00 | 1934400.00 / 47953.00 |\n"
    )
    p = tmp_path / "比利时.md"
    p.write_text(WAGE_8COL.format(rows=rows), encoding="utf-8")
    recs = L.parse_wage_file(p, "比利时")
    assert len(recs) == 3  # 1982 BEF + 2002 BEF/EUR
    by = {(r["year"], r["currency"]): r for r in recs}
    assert by[(1982, "BEF")]["investment_fin_salary"] == 1010595.0
    assert by[(2002, "BEF")]["investment_fin_salary"] == 2340000.0
    assert by[(2002, "EUR")]["investment_fin_salary"] == 58008.0
    assert all(r["cpi_base_year"] == 2013 for r in recs)


def test_wage_japan_cpi_base_2015(tmp_path):
    p = tmp_path / "日本东京.md"
    p.write_text(WAGE_8COL.format(
        rows="| 2002 | 4480000* | JPY | 97.48 | 5790000* | 5370000* | 717000* | 5197000* |\n"),
        encoding="utf-8")
    recs = L.parse_wage_file(p, "日本东京")
    assert recs[0]["cpi_base_year"] == 2015
    assert recs[0]["currency"] == "JPY"


def test_cpi_mixed_separators_including_tky(tmp_path):
    p = tmp_path / "CPI工资.md"
    p.write_text(
        "# 头\n"
        "地区｜年份｜工资增幅｜CPI通胀\n"
        "BEL｜1982｜9.30｜8.73\n"
        "TKY|2002|-1.45|-0.91\n"
        "TKY|2025|2.45|2.10|\n",  # 末行多尾竖线
        encoding="utf-8")
    recs = L.parse_cpi_file(p)
    by = {(r["region"], r["year"]): r for r in recs}
    assert len(recs) == 3
    assert by[("比利时", 1982)]["wage_growth_pct"] == 9.30
    # TKY 半角 + 尾竖线都能解析
    assert by[("日本东京", 2002)]["wage_growth_pct"] == -1.45
    assert by[("日本东京", 2002)]["cpi_pct"] == -0.91
    assert by[("日本东京", 2025)]["cpi_pct"] == 2.10


def test_tax_ignores_second_table(tmp_path):
    """北京 md 有主税率表 + 「北京 vs 上海」对比表（首列含年份），只取主表。"""
    main = (
        "| 年份 | 单位养老 | 单位医疗(含生育) | 单位失业 | 单位工伤(金融一类) | 单位社保合计(不含公积金) | 公积金单位(建模) | 社保月缴费上限(元) | 社保月缴费下限(元) | 政策要点 |\n"
        "| ---- | ---- | ---- | ---- | ---- | ---- | ---- | ---- | ---- | ---- |\n"
        "| 2002 | 20.0% | 10.0% | 1.5% | 0.2% | 31.70% | 8% | 4524 | 905 | 五险框架建立 |\n"
        "| 2003 | 20.0% | 10.0% | 1.5% | 0.2% | 31.70% | 8% | 5235 | 1047 | 费率不变 |\n"
        "\n"
        "## 北京 vs 上海\n"
        "| 项目 | 北京 | 上海 |\n"
        "| 2025年单位社保合计 | 27.80% | 25.70% |\n"
        "| 2002年初始单位社保合计 | 31.70% | 35.20% |\n"
    )
    p = tmp_path / "中国北京.md"
    p.write_text(main, encoding="utf-8")
    recs = L.parse_tax_file(p, "中国北京", "中国北京")
    assert len(recs) == 2                                # 只有 2002/2003 主表，无对比表行
    assert {r["year"] for r in recs} == {2002, 2003}
    assert recs[0]["formula"] == "clamp"
    assert "pension_pct" in recs[0]["params"]
    assert recs[0]["params"]["soc_pct"] == 31.70
    assert recs[0]["params"]["monthly_floor"] == 905.0


def test_tax_uk_fiscal_year_and_params(tmp_path):
    p = tmp_path / "英国.md"
    body = (
        "|财年|雇主NI基准费率(超额部分)|雇主NI次级起征点(年)|法定最低带薪年假(天)|雇主最低养老金缴费|办公室工伤商业险参考费率|备注|\n"
        "|---:|---:|---:|---:|---:|---:|---|\n"
        "|2002-03|11.8%|£4615|20|0%|0.4%|无强制养老金|\n"
        "|2025-26|15.0%|£5000|28|3%|0.4%|2025-04-06 生效|\n"
    )
    p.write_text(body, encoding="utf-8")
    recs = L.parse_tax_file(p, "英国", "英国")
    assert [(r["year"], r["formula"]) for r in recs] == [(2002, "uk_nic"), (2025, "uk_nic")]
    assert recs[0]["params"]["nic_pct"] == 11.8
    assert recs[0]["params"]["nic_threshold"] == 4615.0