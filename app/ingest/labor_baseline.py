"""用工成本基准采集（API② · F-P1-10；DESIGN §13.2）。

issue #218：数据源整合为 基准/公司/用工成本/ 下 2 个汇总文件（原 10 国工资分文件 +
CPI工资.md + 税率/ 12 分文件已删除并入）：
- 用工成本汇总_12地_CPI修正版.md → labor_wage_benchmark（12 地区×年，9 列同构表）
  + labor_cpi_growth（涨薪幅度=名义工资同比%；CPI 同比%由「CPI(2013=100)」定基指数
  相邻年连乘反推：(idx_y/idx_{y-1} − 1)×100，首年无前值 → None）
- 各国雇主社保税率汇总（逐年展开版）.md → labor_tax_benchmark（11 节覆盖 12 office：
  上海节标题「城镇职工/外籍未退休」→ 同参数兼落「中国上海外籍」）

解析边界（已知坑，均有测试锁定）：
- 工资文件按 `## <地区> · …` 节切分；`## 全周期关键指标`（2 列小表）、`## 一、欧洲`
  分区标题、`## 目录`、`## 数据说明` 不是地区节，节内表格一律忽略。
- 税率文件按 `### N. <地区>（…）` 节切分；`## 四、跨国对比总览`（## 级）内表格忽略。
- 税率首列年份支持区间「1982-1983」「2007-2014」「2013-2025」→ 逐年展开（同段参数相同）。
- 双值格「13.8%/15%」「9,100/5,000」（英国 2025 年中改革）取**末值**（年末生效口径）。
- 数值剥离 `*` 估算标记、千分位逗号、`%`/货币符号；「—」/空 → None。
- 节底文字常数（表内无列，整合后不再以独立列存在）：
  荷兰假期津贴 8%（恒定）、美国 FUTA 工资基 $7,000（联邦恒定，加州节文字明示）、
  英国职业养老金 2012 起雇主最低 3%（2012 前无）。
- 三表唯一写入方为本 CLI → 导入为**替换式**（按 office 范围 DELETE 后批量插），天然幂等。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from sqlalchemy.orm import Session

from app.model.labor import LaborCpiGrowth, LaborTaxBenchmark, LaborWageBenchmark

# ---- 常量：数据文件与地区 ------------------------------------------------------
WAGE_SUMMARY_REL = "基准/公司/用工成本/用工成本汇总_12地_CPI修正版.md"
TAX_SUMMARY_REL = "基准/公司/用工成本/各国雇主社保税率汇总（逐年展开版）.md"

# 工资/CPI 12 地区（汇总文件节名；美国拆为纽约/洛杉矶两城、新增北京——#218 起不再有
# 全国口径「美国」工资行，消费侧 labor_cost.LOCATION_ALIAS 同步）
WAGE_REGIONS = ["卢森堡", "比利时", "荷兰", "丹麦", "瑞典", "英国",
                "美国纽约", "美国洛杉矶", "日本东京", "中国香港", "中国上海", "中国北京"]
CPI_BASE_YEAR = 2013   # 汇总文件统一 2013=100（含日本；老 CPI工资.md 日本 2015 口径已废弃）

# 税率 office → 成本模型类型（公式执行器 app/core/labor_cost.py 按此分发）
TAX_OFFICES = {
    "比利时": "onss", "卢森堡": "ccss", "美国洛杉矶": "multi_cap", "美国纽约": "multi_cap",
    "中国上海": "clamp", "中国北京": "clamp", "中国上海外籍": "clamp",
    "英国": "uk_nic", "日本东京": "jp", "丹麦": "headcount", "瑞典": "single_pct", "荷兰": "single_pct_cap",
}

# 税率汇总节标题关键字 → office（顺序敏感：先具体后宽泛；仅在 ### 标题上匹配）
TAX_SECTION_OFFICES: list[tuple[str, str]] = [
    ("比利时", "比利时"),
    ("卢森堡", "卢森堡"),
    ("荷兰", "荷兰"),
    ("丹麦", "丹麦"),
    ("瑞典", "瑞典"),
    ("英国", "英国"),                 # 「### 6. 英国伦敦（…）」
    ("日本", "日本东京"),             # 「### 6. 日本东京（…）」
    ("中国北京", "中国北京"),
    ("中国上海", "中国上海"),         # 节标题含「城镇职工/外籍未退休」→ 兼落外籍 office
    ("美国-加州", "美国洛杉矶"),
    ("加州", "美国洛杉矶"),
    ("美国-纽约", "美国纽约"),
    ("纽约", "美国纽约"),
]


# ---- 工具 -------------------------------------------------------------------
def _num(v: str | None) -> float | None:
    """剥离 `*`/千分位/%/货币符号/括号注释 → float；空/「—」→ None。

    双值格「13.8%/15%」「9,100/5,000」取末段（年中改革取年末生效口径，见模块 docstring）。
    """
    if v is None:
        return None
    s = str(v).strip()
    if "/" in s:                       # 双值：取末值（英国 2025 NIC 费率/阈值）
        s = s.split("/")[-1].strip()
    s = re.sub(r"[%,，£$¥￥€‰]", "", s).replace(",", "").replace(" ", "")
    s = re.sub(r"\(.*?\)", "", s).strip()
    s = s.rstrip("*")
    if not s or s in ("-", "—", "–", "－"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{1,4}:?", c or "") for c in cells)


def _col(headers: list[str], *keywords: str) -> int | None:
    """按表头关键字找列索引（任一关键字命中即取该列）。"""
    for i, h in enumerate(headers):
        if any(k in h for k in keywords):
            return i
    return None


def _year_span(cell: str) -> list[int]:
    """「2002」→[2002]；「1982-1983」「2007-2014」→ 逐年展开；不可解析 → []。"""
    s = str(cell).strip().replace("‑", "-").replace("–", "-").replace("－", "-")
    m = re.fullmatch(r"(\d{4})-(\d{4})", s)
    if m:
        return list(range(int(m.group(1)), int(m.group(2)) + 1))
    m = re.fullmatch(r"(\d{4})", s)
    return [int(m.group(1))] if m else []


# ---- 1. 工资 + CPI（同一汇总文件，按 ## 地区节切分） ------------------------------
def parse_wage_summary(path: Path) -> tuple[list[dict], list[dict]]:
    """12 地汇总 → (labor_wage_benchmark 行, labor_cpi_growth 行)。

    每节一张 9 列同构表：年份|全行业人均名义年薪|涨薪幅度|币种|CPI(2013=100)|
    投资/金融行业年薪|高科技/ICT|企业税费|总用工成本。节末「全周期关键指标」2 列小表
    随下一个非地区 ## 标题（或新地区节）被排除。
    """
    wage_out: list[dict] = []
    cpi_out: list[dict] = []
    region: str | None = None
    headers: list[str] | None = None
    rows: list[tuple] = []

    def flush() -> None:
        nonlocal rows
        if not region or not rows:
            rows = []
            return
        prev_cpi: float | None = None
        for year, avg, growth, currency, cpi_idx, inv in sorted(rows):
            wage_out.append({"region": region, "year": year, "currency": currency,
                             "investment_fin_salary": inv, "avg_salary": avg,
                             "cpi_index": cpi_idx, "cpi_base_year": CPI_BASE_YEAR})
            # CPI 同比%：定基指数相邻年反推（首年/指数缺失 → None）
            cpi_pct = round((cpi_idx / prev_cpi - 1.0) * 100.0, 2) \
                if (prev_cpi and cpi_idx) else None
            cpi_out.append({"region": region, "year": year,
                            "wage_growth_pct": growth, "cpi_pct": cpi_pct})
            prev_cpi = cpi_idx
        rows = []

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            m = re.match(r"^##\s+([^#·\n]+?)\s*·", line)
            new_region = m.group(1).strip() if m else None
            if new_region not in WAGE_REGIONS:
                new_region = None      # 「全周期关键指标」/「一、欧洲」/「目录」/「数据说明」
            if new_region != region:
                flush()
                region = new_region
                headers = None
            continue
        if region is None or "|" not in line:
            continue
        cells = _cells(line)
        if _is_separator(cells):
            continue
        if cells and cells[0] == "年份":
            headers = cells
            continue
        if headers is None:
            continue
        i_year = _col(headers, "年份")
        i_avg = _col(headers, "全行业人均")
        i_growth = _col(headers, "涨薪幅度")
        i_cur = _col(headers, "币种")
        i_cpi = _col(headers, "CPI")
        i_inv = _col(headers, "投资/金融")
        year = _num(cells[i_year]) if i_year is not None else None
        if year is None or year != int(year) or i_cur is None:
            continue
        currency = cells[i_cur].strip()
        if not currency:
            continue
        rows.append((int(year),
                     _num(cells[i_avg]) if i_avg is not None else None,
                     _num(cells[i_growth]) if i_growth is not None else None,
                     currency,
                     _num(cells[i_cpi]) if i_cpi is not None else None,
                     _num(cells[i_inv]) if i_inv is not None else None))
    flush()
    return wage_out, cpi_out


# ---- 2. 税率（按 ### office 节切分，区间年展开） ----------------------------------
def _office_from_title(title: str) -> list[str]:
    """`### N. 英国伦敦（…）` → ["英国"]；上海节 → ["中国上海","中国上海外籍"]。"""
    for kw, office in TAX_SECTION_OFFICES:
        if kw in title:
            if office == "中国上海":
                return ["中国上海", "中国上海外籍"]   # 节标题：城镇职工/外籍未退休
            return [office]
    return []


# 表头关键字 → params 字段（费率存原样%数值，公式执行器 ÷100；上限存原币金额）
_TAX_FIELD_MAP: dict[str, dict[str, tuple[str, ...]]] = {
    "比利时":  {"onss_pct": ("ONSS",), "wc_pct": ("工伤",)},
    "卢森堡":  {"ccss_pct": ("CCSS",), "wc_pct": ("工伤",), "mde_pct": ("MDE", "互助金")},
    "美国洛杉矶": {"oasdi_pct": ("OASDI雇主", "OASDI税率"), "oasdi_cap": ("OASDI工资上限", "OASDI应税"),
                   "medicare_pct": ("Medicare雇主", "Medicare税率", "Medicare税"),
                   "futa_pct": ("FUTA",),
                   "suta_pct": ("加州SUTA",), "suta_cap": ("SUTA上限", "加州SUTA计税"),
                   "wc_pct": ("工伤",)},
    "美国纽约": {"oasdi_pct": ("OASDI雇主", "OASDI税率"), "oasdi_cap": ("OASDI工资上限", "OASDI应税"),
                 "medicare_pct": ("Medicare",),
                 "futa_pct": ("FUTA",),
                 "suta_pct": ("纽约SUTA",), "suta_cap": ("SUTA上限", "纽约州SUTA计税"),
                 "wc_pct": ("工伤",)},
    "中国上海":  {"pension_pct": ("养老",), "medical_pct": ("医疗",), "unemploy_pct": ("失业",),
                  "workinjury_pct": ("工伤",), "soc_pct": ("社保合计",),
                  "housing_pct": ("公积金",), "monthly_cap": ("月缴费上限", "上限"),
                  "monthly_floor": ("月缴费下限", "下限")},
    "中国北京":  {"pension_pct": ("养老",), "medical_pct": ("医疗",), "unemploy_pct": ("失业",),
                  "workinjury_pct": ("工伤",), "soc_pct": ("社保合计",),
                  "housing_pct": ("公积金",), "monthly_cap": ("月缴费上限", "上限"),
                  "monthly_floor": ("月缴费下限", "下限")},
    "中国上海外籍": {"pension_pct": ("养老",), "medical_pct": ("医疗",), "unemploy_pct": ("失业",),
                    "workinjury_pct": ("工伤",), "soc_pct": ("社保合计",),
                    "housing_pct": ("公积金",), "monthly_cap": ("月缴费上限", "上限"),
                    "monthly_floor": ("月缴费下限", "下限")},
    "英国":   {"nic_pct": ("雇主NIC", "雇主NI"), "nic_threshold": ("ST年薪", "起征点")},
    "日本东京": {"kosei_pct": ("厚生年金",), "kenpo_pct": ("健保",), "kaigo_pct": ("介护",),
                 "koyo_pct": ("雇佣保险",), "rosa_pct": ("工伤险", "工伤保险"),
                 "kosei_cap": ("厚生年金月",), "kenpo_cap": ("健保月",)},
    "丹麦":   {"atp_fixed": ("雇主ATP",), "funds_fixed": ("人头基金合计",),
               "total_fixed": ("雇主全部固定人头成本", "法定固定人头总成本"),
               "feriepenge_pct": ("假期工资", "feriepenge"),
               "ferietillaeg_pct": ("假期补贴", "ferietillæg")},
    "瑞典":   {"total_pct": ("雇主社保总包",), "holiday_pct": ("假期附加",)},
    "荷兰":   {"awf_pct": ("AWF",), "whk_pct": ("WHK",), "zvw_pct": ("Zvw", "医保附加"),
               "total_pct": ("雇主合计", "社保合计"),
               "sv_loon_cap": ("SV工资上限", "SV‑loon", "SV-loon"),
               "holiday_pct": ("假期津贴", "假期附加")},
}

# 节底文字常数（表内无列；来源见模块 docstring）
def _section_defaults(office: str, year: int, params: dict) -> dict:
    if office == "荷兰" and params.get("holiday_pct") is None:
        params["holiday_pct"] = 8.0          # 法定假期津贴 8% 年度工资（无上限，全程恒定）
    if office in ("美国洛杉矶", "美国纽约") and params.get("futa_cap") is None:
        params["futa_cap"] = 7000.0         # 联邦 FUTA 工资基 $7,000（永久不变）
    if office == "英国" and year >= 2012 and params.get("pension_pct") is None:
        params["pension_pct"] = 3.0         # 2012 起自动登记职业养老金，雇主最低 3%
    return params


def _extract_tax_params(office: str, row: dict, year: int) -> dict:
    """按 office 表头关键字抽该年费率/上限，叠加节底文字常数；空值剔除。"""
    params: dict = {}
    for key, keywords in _TAX_FIELD_MAP.get(office, {}).items():
        cell = next((v for h, v in row.items() if any(k in h for k in keywords)), None)
        params[key] = _num(cell)
    params = {k: v for k, v in params.items() if v is not None}
    return _section_defaults(office, year, params)


def parse_tax_summary(path: Path) -> list[dict]:
    """税率汇总 → labor_tax_benchmark 行（区间年已展开；上海节兼落外籍 office）。"""
    out: list[dict] = []
    offices: list[str] = []
    headers: list[str] | None = None

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("### "):
            offices = _office_from_title(line)
            headers = None
            continue
        if line.startswith("## "):                    # §四 跨国对比总览等 ## 级节
            offices = []
            headers = None
            continue
        if not offices or "|" not in line:
            continue
        cells = _cells(line)
        if _is_separator(cells):
            continue
        if cells and ("年份" in cells[0]):
            headers = cells
            continue
        if headers is None:
            continue
        years = _year_span(cells[0])
        if not years:
            continue
        row = dict(zip(headers, cells))
        for office in offices:
            for year in years:
                params = _extract_tax_params(office, row, year)
                out.append({"office": office, "year": year,
                            "formula": TAX_OFFICES[office],
                            "params": params, "source_file": path.name})
    return out


# ---- 3. 落库（替换式；三表唯一写入方为本 CLI） ------------------------------------
def _gate(path: Path, source_dir: Path, manifest_active: set[str] | None,
          log, tag: str) -> bool:
    """存在性 + prod 白名单门控；未过门控 → False（调用方计 skipped）。"""
    if not path.exists():
        if log:
            log(f"  [{tag}] 缺文件: {path.relative_to(source_dir)}")
        return False
    if manifest_active is not None and \
            path.relative_to(source_dir).as_posix() not in manifest_active:
        if log:
            log(f"  [{tag}] 未激活: {path.name}")
        return False
    return True


def import_wage_cpi(session: Session, source_dir: Path, log=None,
                    manifest_active: set[str] | None = None) -> dict:
    """工资+CPI 汇总 → 两表全量替换。"""
    stats = {"regions": 0, "wage_rows": 0, "cpi_rows": 0, "skipped": 0}
    path = source_dir / WAGE_SUMMARY_REL
    if not _gate(path, source_dir, manifest_active, log, "wage/cpi"):
        stats["skipped"] += 1
        return stats
    wage_recs, cpi_recs = parse_wage_summary(path)
    session.query(LaborWageBenchmark).delete()
    session.query(LaborCpiGrowth).delete()
    for r in wage_recs:
        session.add(LaborWageBenchmark(**r, source_file=path.name))
    for r in cpi_recs:
        session.add(LaborCpiGrowth(**r, source_file=path.name))
    stats["wage_rows"] = len(wage_recs)
    stats["cpi_rows"] = len(cpi_recs)
    stats["regions"] = len({r["region"] for r in wage_recs})
    if log:
        log(f"  [wage/cpi] {stats['regions']} 地区：工资 {stats['wage_rows']} 行 / "
            f"CPI {stats['cpi_rows']} 行（替换式导入自 {path.name}）")
    return stats


def import_tax(session: Session, source_dir: Path, log=None,
               office_list: Iterable[str] | None = None,
               manifest_active: set[str] | None = None) -> dict:
    """税率汇总 → labor_tax_benchmark；office_list 给定时仅替换指定 office（--office 用）。"""
    stats = {"offices": 0, "rows": 0, "skipped": 0}
    path = source_dir / TAX_SUMMARY_REL
    if not _gate(path, source_dir, manifest_active, log, "tax"):
        stats["skipped"] += 1
        return stats
    recs = parse_tax_summary(path)
    wanted = set(office_list) if office_list else None
    if wanted is not None:
        recs = [r for r in recs if r["office"] in wanted]
        session.query(LaborTaxBenchmark).filter(
            LaborTaxBenchmark.office.in_(wanted)).delete(synchronize_session=False)
    else:
        session.query(LaborTaxBenchmark).delete(synchronize_session=False)
    for r in recs:
        session.add(LaborTaxBenchmark(office=r["office"], year=r["year"],
                                      formula=r["formula"], params=r["params"],
                                      source_file=r["source_file"]))
    stats["rows"] = len(recs)
    stats["offices"] = len({r["office"] for r in recs})
    if log:
        log(f"  [tax] {stats['offices']} office：{stats['rows']} 行（替换式导入自 {path.name}）")
    return stats


def import_labor_baseline(session: Session, source_dir: Path, log=None,
                          manifest_active: set[str] | None = None) -> dict:
    """三块基准一体落库（替换式，不含 commit；由命令层 commit）。"""
    return {
        "wage_cpi": import_wage_cpi(session, source_dir, log, manifest_active),
        "tax": import_tax(session, source_dir, log, None, manifest_active),
    }
