"""用工成本基准采集（API② · F-P1-10；DESIGN §13.2）。

把 Design_Folder 三份基准解析落库：
- labor_wage_benchmark：工资表（10 区×年）——投资/金融年薪 + 全行业人均 + CPI 定基
- labor_cpi_growth：CPI工资.md（10 区×年）——工资增幅%/通胀%
- labor_tax_benchmark：税率表（12 office×年）——异结构费率/上限 JSONB + 成本模型类型

解析边界（已知坑，均有测试锁定）：
- 数值剥离 `*` 估算标记、千分位逗号。
- CPI工资.md **全角 `｜` 与半角 `|` 混用**（前 9 区全角、东京段半角且末行多尾竖线）。
- 比/卢 2002 行 `BEF/EUR` 双币 → 拆分存两行。
- 日本 CPI 定基 2015≈别家 2013 → cpi_base_year 按区标。
- 英国税率按财年 `2002-03` → year 取起始年。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.model.labor import LaborCpiGrowth, LaborTaxBenchmark, LaborWageBenchmark

# ---- 常量：工资区（文件名） × 汇率/基年 ----------------------------------------
WAGE_REGIONS = ["比利时", "卢森堡", "荷兰", "瑞典", "丹麦", "美国", "英国",
                "中国香港", "中国上海", "日本东京"]
CPI_BASE_YEAR = {r: (2015 if r == "日本东京" else 2013) for r in WAGE_REGIONS}

# CPI工资.md 地区码 → 中文区名
CPI_CODE_TO_REGION = {
    "BEL": "比利时", "LUX": "卢森堡", "NLD": "荷兰", "SWE": "瑞典", "DNK": "丹麦",
    "USA": "美国", "GBR": "英国", "HKG": "中国香港", "SHA": "中国上海", "TKY": "日本东京",
}

# 税率 office（文件名） → (成本模型类型, 用于解析的字段键掩码描述)
# 键语义：_pct=百分比费率(文件原样数值，公式除以100)；_cap=工资上限(原币金额)。
TAX_OFFICES = {
    "比利时": "onss", "卢森堡": "ccss", "美国洛杉矶": "multi_cap", "美国纽约": "multi_cap",
    "中国上海": "clamp", "中国北京": "clamp", "中国上海外籍": "clamp",
    "英国": "uk_nic", "日本东京": "jp", "丹麦": "headcount", "瑞典": "single_pct", "荷兰": "single_pct_cap",
}


# ---- 工具 -------------------------------------------------------------------
def _num(v: str) -> float | None:
    """剥离 `*`/千分位/%、单位括号注释 → float；空 → None。"""
    if v is None:
        return None
    s = str(v).strip()
    # 去掉年度范围（"2002‑03"）只取头年份的解析单独处理
    s = re.sub(r"[%,，£$¥￥€‰]", "", s).replace(",", "").replace(" ", "")
    s = re.sub(r"\(.*?\)", "", s).strip()
    s = s.rstrip("*")
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _fiscal_year(v: str) -> int | None:
    """财年 "2002‑03"/"2002-03"/"2002‑2025" → 起始年 int；否则 None。"""
    s = str(v).strip()
    m = re.match(r"^(\d{4})", s.replace("‑", "-").replace("－", "-"))
    return int(m.group(1)) if m else None


def _table_rows(lines: Iterable[str]) -> list[list[str]]:
    """提取 markdown **第一张**表行（首个|分隔）；跳过表头分隔行 ---。

    只取第一个连续表块（遇空行/非|行即停），避免把文件里第二张说明/对比表误并入
    （如北京 md 的「北京 vs 上海」对比表首列含年份，会与主表产生重复键）。"""
    rows: list[list[str]] = []
    started = False
    for line in lines:
        if "|" not in line:
            if started and rows:  # 第一张表结束
                break
            continue
        started = True
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{1,4}:?", c or "") for c in cells):
            continue
        rows.append(cells)
    return rows


def _table_as_map(rows: list[list[str]]) -> list[dict]:
    """首行作表头 → 每数据行 dict[header]=cell。"""
    if not rows:
        return []
    headers = rows[0]
    return [dict(zip(headers, r)) for r in rows[1:]]


def _col_index_by_keyword(headers: list[str], keywords: tuple[str, ...]) -> int | None:
    """按表头关键字找列索引。"""
    for i, h in enumerate(headers):
        if any(k in h for k in keywords):
            return i
    return None


# ---- 1. 工资表 ---------------------------------------------------------------
def parse_wage_file(path: Path, region: str) -> list[dict]:
    rows = _table_rows(path.read_text(encoding="utf-8").splitlines())
    out: list[dict] = []
    for r in rows[1:]:
        year = _num(r[0])
        if year is None or year != int(year):
            continue
        cur_parts = [p.strip() for p in r[2].split("/")]
        inv_parts = [p.strip() for p in r[4].split("/")]
        # 比/卢 2002 双币：拆两行
        if len(cur_parts) == 2:
            for cur, inv in zip(cur_parts, inv_parts):
                out.append({"region": region, "year": int(year), "currency": cur,
                            "investment_fin_salary": _num(inv), "avg_salary": _num(r[1]),
                            "cpi_index": _num(r[3]), "cpi_base_year": CPI_BASE_YEAR[region]})
        else:
            out.append({"region": region, "year": int(year), "currency": cur_parts[0],
                        "investment_fin_salary": _num(inv_parts[0]), "avg_salary": _num(r[1]),
                        "cpi_index": _num(r[3]), "cpi_base_year": CPI_BASE_YEAR[region]})
    return out


def import_wage(session: Session, source_dir: Path, log=None) -> dict:
    stats = {"region": 0, "rows": 0, "skipped": 0}
    base = source_dir / "基准" / "公司" / "用工成本"
    for region in WAGE_REGIONS:
        path = base / f"{region}.md"
        if not path.exists():
            if log: log(f"  [wage] 缺文件: {region}.md")
            stats["skipped"] += 1
            continue
        recs = parse_wage_file(path, region)
        for r in recs:
            dup = session.execute(select(LaborWageBenchmark.id).where(
                LaborWageBenchmark.region == r["region"],
                LaborWageBenchmark.year == r["year"],
                LaborWageBenchmark.currency == r["currency"]).limit(1)).scalar_one_or_none()
            if dup is None:
                session.add(LaborWageBenchmark(**r, source_file=str(path)))
                stats["rows"] += 1
        stats["region"] += 1
    return stats


# ---- 2. CPI 指数 -------------------------------------------------------------
def parse_cpi_file(path: Path) -> list[dict]:
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        # 兼容全角 ｜ 与半角 | 双分隔符
        parts = [p.strip() for p in re.split(r"[｜|]", line) if p.strip()]
        if len(parts) != 4:
            continue
        code, year, wg, cpi = parts
        region = CPI_CODE_TO_REGION.get(code)
        if region is None or not re.fullmatch(r"\d{4}", year):
            continue
        out.append({"region": region, "year": int(year),
                    "wage_growth_pct": _num(wg), "cpi_pct": _num(cpi)})
    return out


def import_cpi(session: Session, source_dir: Path, log=None) -> dict:
    path = source_dir / "基准" / "CPI工资.md"
    stats = {"rows": 0, "skipped": 0, "regions": set()}
    if not path.exists():
        stats["skipped"] += 1
        # issue #144：缺文件早退时归一为 int，避免上层打印 set()
        stats["regions"] = 0
        return stats
    for r in parse_cpi_file(path):
        dup = session.execute(select(LaborCpiGrowth.id).where(
            LaborCpiGrowth.region == r["region"],
            LaborCpiGrowth.year == r["year"]).limit(1)).scalar_one_or_none()
        if dup is None:
            session.add(LaborCpiGrowth(**r))
            stats["rows"] += 1
        stats["regions"].add(r["region"])
    stats["regions"] = len(stats["regions"])
    return stats


# ---- 3. 税率表 ---------------------------------------------------------------
def _extract_tax_params(office: str, row: dict) -> dict:
    """按 office 的字段键映射，从表头 dict 抽取该年费率/上限。费率存原样数值(%)，上限存原币金额。"""
    params: dict = {}
    field_map = {
        "比利时":  {"onss_pct": ("ONSS",), "wc_pct": ("工伤",)},
        "卢森堡":  {"ccss_pct": ("CCSS",), "wc_pct": ("工伤",), "mde_pct": ("MDE", "互助金")},
        "美国洛杉矶": {"oasdi_pct": ("OASDI税率",), "oasdi_cap": ("OASDI应税",),
                       "medicare_pct": ("Medicare税率", "Medicare税"), "futa_pct": ("FUTA实际",),
                       "futa_cap": ("FUTA计税",), "suta_pct": ("加州参考SUTA", "加州SUTA"),
                       "suta_cap": ("加州SUTA计税",), "wc_pct": ("工伤",)},
        "美国纽约": {"oasdi_pct": ("OASDI税率",), "oasdi_cap": ("OASDI应税",),
                     "medicare_pct": ("Medicare税率", "Medicare税"), "futa_pct": ("FUTA实际",),
                     "futa_cap": ("FUTA计税",), "suta_pct": ("纽约州SUTA新雇主", "纽约州SUTA税率"),
                     "suta_cap": ("纽约州SUTA计税",), "wc_pct": ("工伤",)},
        "中国上海":  {"pension_pct": ("养老",), "medical_pct": ("医疗",), "unemploy_pct": ("失业",),
                      "workinjury_pct": ("工伤",), "soc_pct": ("社保合计",),
                      "housing_pct": ("公积金单位",), "monthly_cap": ("上限",), "monthly_floor": ("下限",)},
        "中国北京":  {"pension_pct": ("养老",), "medical_pct": ("医疗",), "unemploy_pct": ("失业",),
                      "workinjury_pct": ("工伤",), "soc_pct": ("社保合计",),
                      "housing_pct": ("公积金单位",), "monthly_cap": ("上限",), "monthly_floor": ("下限",)},
        "中国上海外籍": {"pension_pct": ("养老",), "medical_pct": ("医疗",), "unemploy_pct": ("失业",),
                         "workinjury_pct": ("工伤",), "soc_pct": ("社保合计",),
                         "housing_pct": ("公积金单位",), "monthly_cap": ("上限",), "monthly_floor": ("下限",)},
        "英国":   {"nic_pct": ("雇主NI基准", "雇主NI费率"), "nic_threshold": ("起征点",),
                   "pension_pct": ("最低养老金",), "wc_pct": ("工伤",)},
        "日本东京": {"kosei_pct": ("厚生年金",), "kenpo_pct": ("健保",), "kaigo_pct": ("介护",),
                     "koyo_pct": ("雇佣保险",), "rosa_pct": ("工伤保险",),
                     "kosei_cap": ("厚生年金月",), "kenpo_cap": ("健保月",)},
        "丹麦":   {"atp_fixed": ("雇主ATP",), "funds_fixed": ("人头基金合计",),
                   "total_fixed": ("法定固定人头总成本",), "feriepenge_pct": ("feriepenge", "假期工资"),
                   "ferietillaeg_pct": ("ferietillæg", "假期补贴")},
        "瑞典":   {"total_pct": ("雇主社保总包",), "holiday_pct": ("假期附加",)},
        "荷兰":   {"awf_pct": ("AWF",), "whk_pct": ("WHK",), "zvw_pct": ("Zvw", "医保附加"),
                   "total_pct": ("社保合计",), "sv_loon_cap": ("SV‑loon", "SV-loon", "SV‑loon"),
                   "holiday_pct": ("假期津贴",)},
    }
    for key, keywords in field_map.get(office, {}).items():
        cell = next((v for h, v in row.items() if any(k in h for k in keywords)), None)
        params[key] = _num(cell)
    return {k: v for k, v in params.items() if v is not None}


def parse_tax_file(path: Path, office: str, country_or_region: str) -> list[dict]:
    rows = _table_rows(path.read_text(encoding="utf-8").splitlines())
    data = _table_as_map(rows)
    out: list[dict] = []
    for row in data:
        yr_cell = next(iter(row.values()))
        year = _fiscal_year(yr_cell)  # 含 "2002‑03" 财年；纯年份也命中
        if year is None or year < 1900:
            year = _num(yr_cell)
        if year is None or year != int(year):
            continue
        out.append({"office": office, "year": int(year),
                    "formula": TAX_OFFICES[office],
                    "params": _extract_tax_params(office, row) or {},
                    "source_file": str(path)})
    return out


def import_tax(session: Session, source_dir: Path, log=None,
               office_list: Iterable[str] | None = None) -> dict:
    stats = {"office": 0, "rows": 0, "skipped": 0}
    base = source_dir / "基准" / "公司" / "用工成本" / "税率"
    for office in (office_list or TAX_OFFICES):
        path = base / f"{office}.md"
        if not path.exists():
            if log: log(f"  [tax] 缺文件: {office}.md")
            stats["skipped"] += 1
            continue
        for r in parse_tax_file(path, office, office):
            dup = session.execute(select(LaborTaxBenchmark.id).where(
                LaborTaxBenchmark.office == r["office"],
                LaborTaxBenchmark.year == r["year"]).limit(1)).scalar_one_or_none()
            if dup is None:
                session.add(LaborTaxBenchmark(office=r["office"], year=r["year"],
                                              formula=r["formula"], params=r["params"],
                                              source_file=r["source_file"]))
                stats["rows"] += 1
        stats["office"] += 1
    return stats


def import_labor_baseline(session: Session, source_dir: Path, log=None) -> dict:
    """三块基准一体落库（幂等，不含 commit；由命令层 commit）。"""
    return {
        "wage": import_wage(session, source_dir, log),
        "cpi": import_cpi(session, source_dir, log),
        "tax": import_tax(session, source_dir, log),
    }