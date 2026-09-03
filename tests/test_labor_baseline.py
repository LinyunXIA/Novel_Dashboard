"""用工成本基准解析器单测（API② · F-P1-10；issue #218 汇总文件版）。

数据源已整合为 基准/公司/用工成本/ 下 2 个汇总文件：
- 用工成本汇总_12地_CPI修正版.md（## 地区·节，9 列同构表）→ wage + cpi
- 各国雇主社保税率汇总（逐年展开版）.md（### N. office 节，异构表）→ tax

覆盖坑：
- 工资：## 节切分（含「全周期关键指标」2 列小表 / 「## 一、欧洲」分区标题排除）、
  涨薪「—」→ None、CPI 同比由定基指数相邻年反推（首年 None）、2013 统一基年（含日本）。
- 税率：### 节切分（## 对比总览排除）、区间年展开（1982-1983 / 2017-2025）、
  双值取末值（英国 2025 13.8%/15% → 15%；9,100/5,000 → 5,000）、
  ST 年薪列作起征点（不取周薪 89）、上海节兼落外籍、节级常数（荷兰 8%/美国 7000/英国 2012+ 3%）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.ingest import labor_baseline as L

WAGE_HEADER = ("| 年份 | 全行业人均名义年薪 | 涨薪幅度 | 币种 | CPI(2013=100) | "
               "投资/金融行业年薪 | 高科技/ICT行业年薪 | 企业年度税费总和 | 总用工成本（税前+税费） |\n"
               "| ---- | ------------------ | -------- | ---- | ------------- | "
               "---------------- | ---------------- | ---------------- | ---------------------- |\n")

WAGE_DOC = (
    "# 全球各国/地区 · 汇总（CPI统一修正版）\n"
    "## 目录\n"
    "### 一、欧洲\n"
    "- 卢森堡（1982-2025，LUF→EUR）\n"
    "---\n"
    "## 一、欧洲\n"
    "## 卢森堡 · 人均薪资 / CPI / 涨薪幅度 / 行业薪资 / 用工成本（1982-2025）\n"
    "> 口径说明\n"
    f"{WAGE_HEADER}"
    "| 1982 | 887,478 | — | LUF | 42.57 | 1,286,843 | 1,153,721 | 128,684 | 1,016,162 |\n"
    "| 1983 | 927,818 | +4.55% | LUF | 46.58 | 1,345,336 | 1,206,163 | 134,070 | 1,061,888 |\n"
    "| 2002 | 52,000 | +6.12% | EUR | 77.51 | 75,400 | 67,600 | 7,020 | 59,020 |\n"
    "\n"
    "## 全周期关键指标\n"
    "| 指标 | 数值 |\n"
    "|------|------|\n"
    "| 1982-2025名义工资CAGR | +-5.71% |\n"
    "| 货币切换年 | 2002年（LUF→EUR） |\n"
    "---\n"
    "## 日本东京 · 人均薪资 / CPI / 涨薪幅度 / 行业薪资 / 用工成本（2002-2025）\n"
    f"{WAGE_HEADER}"
    "| 2002 | 4,480,000 | — | JPY | 97.48 | 5,790,000 | 5,370,000 | 717,000 | 5,197,000 |\n"
    "## 数据说明\n"
    "> 数据来源：OECD、世界银行\n"
)

TAX_DOC = (
    "# 各国/城市雇主社保税率汇总（逐年展开版）\n"
    "## 一、欧洲五国\n"
    "### 1. 比利时（白领CP200，雇主ONSS基准费率）\n"
    "| 年份区间 | ONSS基准 | 工伤险 | 雇主合计 | 政策节点 |\n"
    "|---------|---------|--------|---------|---------|\n"
    "| 1982-1983 | 31.50% | 0.32% | 31.82% | 全工资基数全额缴纳 |\n"
    "| 2017-2025 | 27.20% | 0.32% | 27.52% | Tax-shift完成，稳定 |\n"
    "- **附加成本**：双倍假期工资92%月薪 + 十三薪\n"
    "---\n"
    "### 6. 英国伦敦（雇主国民保险NIC，逐年展开）\n"
    "| 年份 | 雇主NIC费率 | ST周薪(GBP) | ST年薪(GBP) | 政策节点 |\n"
    "|------|------------|------------|------------|---------|\n"
    "| 2002 | 12.2% | 89 | 4,628 | 标准费率 |\n"
    "| 2011 | 13.8% | 136 | 7,072 | 4月费率上调 |\n"
    "| 2025 | 13.8%/15% | 175/96 | 9,100/5,000 | 4月起15% |\n"
    "- **养老金**：2012年起自动登记职业养老金，雇主最低3%\n"
    "---\n"
    "### 8. 中国上海（城镇职工/外籍未退休，逐年展开）\n"
    "| 年份 | 养老 | 医疗(含生育) | 失业 | 工伤 | 社保合计 | 公积金 | 补充公积金(单位) | 月缴费上限(元) | 年缴费上限(元) |\n"
    "|------|------|------------|------|------|---------|--------|----------------|--------------|--------------|\n"
    "| 2002 | 20.0% | 10.0% | 1.5% | 0.2% | 31.70% | 7% | 1%-5%自愿(常见3%) | 4,524 | 54,288 |\n"
    "| 2025 | 16.0% | 9.0% | 0.5% | 0.2% | 25.70% | 7% | 1%-5%自愿(常见3%) | 37,302 | 447,624 |\n"
    "---\n"
    "### 3. 荷兰（永久全职合同，雇主社保合计）\n"
    "| 年份 | AWF-WW失业 | WHK残疾 | Zvw医保 | 雇主合计 | SV工资上限(€/年) |\n"
    "|------|-----------|---------|---------|---------|-----------------|\n"
    "| 2008 | 2.30% | 1.40% | 5.65% | 9.35% | 44,041 |\n"
    "| 2025 | 2.64% | 1.40% | 6.10% | 10.14% | 75,864 |\n"
    "- **法定假期津贴**：8%年度工资（无上限）\n"
    "---\n"
    "### 9. 美国-加州（洛杉矶，逐年展开）\n"
    "| 年份 | OASDI雇主 | OASDI工资上限(USD) | Medicare雇主 | FUTA | 加州SUTA | SUTA上限 | 工伤险 |\n"
    "|------|----------|-------------------|-------------|------|---------|---------|--------|\n"
    "| 2025 | 6.20% | 176,100 | 1.45% | 0.60% | 3.40% | 7,000 | 0.45% |\n"
    "---\n"
    "## 四、跨国对比总览（2025年基准）\n"
    "| 国家/城市 | 雇主社保模式 | 雇主总费率/成本 |\n"
    "|----------|------------|---------------|\n"
    "| 比利时 | 工资百分比 | ~27.52% |\n"
    "| 英国伦敦 | 工资百分比 | 15.0%(2025.4起) |\n"
)


def _write_summaries(tmp: Path) -> tuple[Path, Path]:
    base = tmp / "基准" / "公司" / "用工成本"
    base.mkdir(parents=True)
    wage = base / "用工成本汇总_12地_CPI修正版.md"
    tax = base / "各国雇主社保税率汇总（逐年展开版）.md"
    wage.write_text(WAGE_DOC, encoding="utf-8")
    tax.write_text(TAX_DOC, encoding="utf-8")
    return wage, tax


# ---- 工资 + CPI 汇总 ----------------------------------------------------------
def test_parse_wage_summary(tmp_path):
    wage_path, _ = _write_summaries(tmp_path)
    wage_recs, cpi_recs = L.parse_wage_summary(wage_path)

    # 卢森堡 3 行（LUF×2 + EUR×1）+ 东京 1 行；「全周期关键指标」/目录/数据说明表不入
    assert len(wage_recs) == 4
    by = {(r["region"], r["year"], r["currency"]): r for r in wage_recs}
    assert by[("卢森堡", 1982, "LUF")]["avg_salary"] == 887478.0
    assert by[("卢森堡", 1982, "LUF")]["investment_fin_salary"] == 1286843.0
    assert by[("卢森堡", 2002, "EUR")]["avg_salary"] == 52000.0
    assert by[("日本东京", 2002, "JPY")]["investment_fin_salary"] == 5790000.0
    # 新汇总统一 2013 基年（老口径日本 2015 已废弃）
    assert all(r["cpi_base_year"] == 2013 for r in wage_recs)
    assert all(r["cpi_index"] is not None for r in wage_recs)

    # CPI：涨薪幅度 → wage_growth_pct；同比由定基指数反推
    assert len(cpi_recs) == 4
    cby = {(r["region"], r["year"]): r for r in cpi_recs}
    assert cby[("卢森堡", 1982)]["wage_growth_pct"] is None     # 「—」
    assert cby[("卢森堡", 1982)]["cpi_pct"] is None             # 首年无前值
    assert cby[("卢森堡", 1983)]["wage_growth_pct"] == 4.55
    assert cby[("卢森堡", 1983)]["cpi_pct"] == pytest.approx(
        (46.58 / 42.57 - 1) * 100, abs=0.01)
    assert cby[("卢森堡", 2002)]["wage_growth_pct"] == 6.12
    assert cby[("日本东京", 2002)]["cpi_pct"] is None           # 东京节首年


def test_parse_tax_summary(tmp_path):
    _, tax_path = _write_summaries(tmp_path)
    recs = L.parse_tax_summary(tax_path)

    by = {(r["office"], r["year"]): r for r in recs}
    # 比利时区间展开：1982-1983 + 2017-2025（11 年）
    be_years = {y for (o, y) in by if o == "比利时"}
    assert be_years == {1982, 1983, *range(2017, 2026)}
    assert by[("比利时", 1982)]["formula"] == "onss"
    assert by[("比利时", 1982)]["params"]["onss_pct"] == 31.5
    assert by[("比利时", 2017)]["params"]["onss_pct"] == 27.2
    assert by[("比利时", 2025)]["params"]["wc_pct"] == 0.32

    # 英国：ST 年薪列作起征点（非周薪 89）；2025 双值取末值；养老金 2012 起 3%
    uk = {y: by[("英国", y)] for y in (2002, 2011, 2025)}
    assert uk[2002]["params"]["nic_pct"] == 12.2
    assert uk[2002]["params"]["nic_threshold"] == 4628.0
    assert "pension_pct" not in uk[2002]["params"]              # 2012 前无养老金
    assert "pension_pct" not in uk[2011]["params"]              # 2012 起才自动登记
    assert uk[2011]["params"]["nic_pct"] == 13.8
    assert uk[2025]["params"]["nic_pct"] == 15.0                # 13.8%/15% → 末值
    assert uk[2025]["params"]["nic_threshold"] == 5000.0        # 9,100/5,000 → 末值
    assert uk[2025]["params"]["pension_pct"] == 3.0             # 2012 起雇主最低 3%
    assert uk[2025]["formula"] == "uk_nic"

    # 上海节兼落外籍 office，参数相同
    for office in ("中国上海", "中国上海外籍"):
        r = by[(office, 2025)]
        assert r["formula"] == "clamp"
        assert r["params"]["soc_pct"] == 25.7
        assert r["params"]["housing_pct"] == 7.0
        assert r["params"]["monthly_cap"] == 37302.0
        assert "monthly_floor" not in r["params"]               # 新表无下限列
    assert by[("中国上海", 2002)]["params"]["soc_pct"] == 31.7

    # 荷兰：节底文字常数 假期津贴 8%；SV 上限取「SV工资上限」列
    nl = by[("荷兰", 2025)]
    assert nl["formula"] == "single_pct_cap"
    assert nl["params"]["holiday_pct"] == 8.0
    assert nl["params"]["sv_loon_cap"] == 75864.0
    assert nl["params"]["total_pct"] == 10.14
    assert by[("荷兰", 2008)]["params"]["sv_loon_cap"] == 44041.0

    # 美国洛杉矶：FUTA 工资基常数 7,000；OASDI/SUTA 上限取列
    la = by[("美国洛杉矶", 2025)]
    assert la["formula"] == "multi_cap"
    assert la["params"]["futa_cap"] == 7000.0
    assert la["params"]["oasdi_cap"] == 176100.0
    assert la["params"]["suta_cap"] == 7000.0
    assert la["params"]["oasdi_pct"] == 6.2
    assert la["params"]["medicare_pct"] == 1.45

    # §四 跨国对比总览（## 级）不入；office 集合恰为 5 节（上海节算 2 office）
    assert {o for (o, _y) in by} == {
        "比利时", "英国", "中国上海", "中国上海外籍", "荷兰", "美国洛杉矶"}


# ---- 落库：替换式幂等 + 门控 ----------------------------------------------------
@pytest.fixture()
def session():
    from sqlalchemy import BigInteger, Integer
    from app.model import Base
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


def test_import_labor_baseline_replace_idempotent(tmp_path, session):
    from app.model.labor import LaborCpiGrowth, LaborTaxBenchmark, LaborWageBenchmark
    _write_summaries(tmp_path)

    def counts():
        return (session.execute(select(func.count()).select_from(LaborWageBenchmark)).scalar(),
                session.execute(select(func.count()).select_from(LaborCpiGrowth)).scalar(),
                session.execute(select(func.count()).select_from(LaborTaxBenchmark)).scalar())

    r1 = L.import_labor_baseline(session, tmp_path)
    n_wage, n_cpi, n_tax = counts()
    assert n_wage == 4 and n_cpi == 4
    # 比利时 11 + 英国 3 + 上海节 2 office×2 年=4 + 荷兰 2 + 洛杉矶 1 = 21
    assert n_tax == 21
    assert r1["wage_cpi"]["regions"] == 2 and r1["tax"]["offices"] == 6

    # 二跑：替换式，行数不增
    L.import_labor_baseline(session, tmp_path)
    assert counts() == (n_wage, n_cpi, n_tax)
    # 溯源全部指向新汇总文件
    srcs = {x[0] for x in session.execute(
        select(LaborWageBenchmark.source_file).distinct()).all()}
    assert srcs == {"用工成本汇总_12地_CPI修正版.md"}
    tsrcs = {x[0] for x in session.execute(
        select(LaborTaxBenchmark.source_file).distinct()).all()}
    assert tsrcs == {"各国雇主社保税率汇总（逐年展开版）.md"}


def test_import_tax_office_scoped_replace(tmp_path, session):
    """--office 模式：仅替换指定 office，其他 office 行保留。"""
    from app.model.labor import LaborTaxBenchmark
    _write_summaries(tmp_path)
    L.import_labor_baseline(session, tmp_path)
    before = session.execute(select(func.count()).select_from(LaborTaxBenchmark)).scalar()

    r = L.import_tax(session, tmp_path, office_list=["英国"])
    assert r["rows"] == 3
    after = session.execute(select(func.count()).select_from(LaborTaxBenchmark)).scalar()
    assert after == before                    # 英国 3 行被删后重插，总数不变
    offices = {x[0] for x in session.execute(
        select(LaborTaxBenchmark.office).distinct()).all()}
    assert "英国" in offices and "比利时" in offices


def test_import_gated_by_manifest(tmp_path, session):
    """prod 白名单未激活 → skipped，表保持空。"""
    from app.model.labor import LaborCpiGrowth, LaborTaxBenchmark, LaborWageBenchmark
    _write_summaries(tmp_path)
    r = L.import_labor_baseline(session, tmp_path, manifest_active=set())
    assert r["wage_cpi"]["skipped"] == 1 and r["tax"]["skipped"] == 1
    for m in (LaborWageBenchmark, LaborCpiGrowth, LaborTaxBenchmark):
        assert session.execute(select(func.count()).select_from(m)).scalar() == 0
