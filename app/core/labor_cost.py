"""用工成本核心（API② · F-P1-10；DESIGN §13.2）。

成本公式（用户确认版）：
  定位：work_location → region(工资+CPI) + office(税率) + 奖金月数
  内部全职：基准年薪 = 投资/金融年薪(岗位opening年) × (1+Level%)
            逐年增幅：salary ×= (1 + CPI工资增幅/y)  增幅<0 → 不涨
  外包类：基准 = 当年全行业人均 × 系数(可选1.05 / 法律强制外包1.2)，每年现查不累积
  基本用工成本 = 该 office 税率公式(当年 salary)      # 费率表 + 说明文字隐藏项
  固定奖金：日本 3 月，其余 2 月 → (salary/12)×月数
  总成本 = salary + 基本用工成本 + 奖金；在岗月折算 ×(交叠月/12)

税率公式细节与税率表的"说明文字隐藏项"（比利时十三薪/双倍假期、英国学徒税等）
只在后台算，不进入 UI（UI 只显示加薪规则，见 rules_payload）。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.model.labor import LaborCpiGrowth, LaborTaxBenchmark, LaborWageBenchmark

# --------------------------------------------------------------------------
# 规则表（也是 UI「加薪规则」屏的数据源，rules_payload()）
# --------------------------------------------------------------------------
# Level → 基础年薪调整%（用户确认：M11b/M11c=5% 非笔误）
LEVEL_PCT: dict[str, float] = {
    "B6": .05, "B7a": .10, "B7b": .15, "M7": .15, "B8a": .20, "M8a": .20,
    "B8b": .30, "M8b": .30, "B9a": .40, "M9a": .40, "M9b": .50,
    "B9b": .60, "M9c": .60, "B10": .70, "M10a": .70, "M10b": .80,
    "M10c": .90, "M11a": 1.00, "M11b": .05, "M11c": .05,
}
# legal_category → 外包基准系数（对应"全行业人均名义年薪"）
OUTSOURCE_FACTOR: dict[str, float] = {
    "可选（集团内控推荐）": 1.05,
    "法律强制·允许第三方外包": 1.2,
}
BONUS_MONTHS_DEFAULT = 2
BONUS_MONTHS_JAPAN = 3
PROMOTION_STEP_PCT = 0.05  # 晋升：每跨一级 5%

# work_location → (region 工资/CPI, office 税率, bonus 月数)。
# ⚠ 初始初稿，外部 work_location 实际取值待用户给清单后校正（尤其 北京→上海工资代理、香港无税率 office）。
LOCATION_ALIAS: list[tuple[str, str, str | None, int]] = [
    ("卢森堡", "卢森堡", "卢森堡", 2),
    ("布鲁塞尔", "比利时", "比利时", 2),
    ("比利时", "比利时", "比利时", 2),
    ("纽约", "美国", "美国纽约", 2),
    ("洛杉矶", "美国", "美国洛杉矶", 2),
    ("美国", "美国", "美国洛杉矶", 2),
    ("东京", "日本东京", "日本东京", BONUS_MONTHS_JAPAN),   # 日本 3 月
    ("上海", "中国上海", "中国上海", 2),
    ("北京", "中国上海", "中国北京", 2),                      # 工资用中国上海代理；office 北京税率
    ("伦敦", "英国", "英国", 2),
    ("英国", "英国", "英国", 2),
    ("斯德哥尔摩", "瑞典", "瑞典", 2),
    ("哥本哈根", "丹麦", "丹麦", 2),
    ("阿姆斯特丹", "荷兰", "荷兰", 2),
    ("香港", "中国香港", None, 2),                            # 无税率 office → 成本缺
]

IS_OUTSOURCED_MARKERS = ("Outsourced", "External")          # position_name/type 命中即外包


def locate(work_location: str) -> tuple[str, str | None, int] | None:
    """work_location → (region, office, bonus_months)；未匹配 → None。"""
    wl = work_location or ""
    for frag, r, o, bm in LOCATION_ALIAS:
        if frag in wl:
            return r, o, bm
    return None


def _locate(pos: dict) -> tuple[str, str | None, int] | None:
    """定位：work_location 优先；未命中退回 country_or_region（用户确认法则2）。
    country_or_region 形如 "Country·卢森堡"（子串命中）。"""
    loc = locate(pos.get("work_location") or "")
    if loc is None:
        loc = locate(pos.get("country_or_region") or "")
    return loc


def is_outsourced(pos: dict) -> bool:
    txt = f"{pos.get('position_type','')} {pos.get('position_name','')}"
    return any(m in txt for m in IS_OUTSOURCED_MARKERS)


# --------------------------------------------------------------------------
# DB 读基准
# --------------------------------------------------------------------------
def _wage_row(db: Session, region: str, year: int):
    """region×year 工资行；2002(BEF/EUR 双币)及2003+ 偏好 EUR，否则该年实际币种(BEF/LUF…)。"""
    rows = db.execute(select(LaborWageBenchmark).where(
        LaborWageBenchmark.region == region, LaborWageBenchmark.year == year)).scalars().all()
    if not rows:
        return None
    for r in rows:
        if r.currency == "EUR":
            return r
    return rows[0]


def investment_fin_salary(db: Session, region: str, year: int) -> float | None:
    w = _wage_row(db, region, year)
    return float(w.investment_fin_salary) if w and w.investment_fin_salary is not None else None


def avg_salary(db: Session, region: str, year: int) -> float | None:
    w = _wage_row(db, region, year)
    return float(w.avg_salary) if w and w.avg_salary is not None else None


def wage_growth_pct(db: Session, region: str, year: int) -> float:
    g = db.execute(select(LaborCpiGrowth.wage_growth_pct).where(
        LaborCpiGrowth.region == region, LaborCpiGrowth.year == year)).scalar_one_or_none()
    return float(g) if g is not None else 0.0


def _tax_row(db: Session, office: str, year: int):
    return db.execute(select(LaborTaxBenchmark).where(
        LaborTaxBenchmark.office == office, LaborTaxBenchmark.year == year)).scalar_one_or_none()


# --------------------------------------------------------------------------
# 税率公式执行器（费率存原样%数值 → ÷100；上限/人头存原币金额）
# --------------------------------------------------------------------------
def _pct(x) -> float:
    return (float(x) if x is not None else 0.0) / 100.0


def _clamp_annual(salary: float, monthly_floor, monthly_cap) -> float:
    """中国/日本 clamp：月税前工资 clamp 上下限 → ×12 年度基数。"""
    month = salary / 12.0
    if monthly_floor is not None:
        month = max(month, float(monthly_floor))
    if monthly_cap is not None:
        month = min(month, float(monthly_cap))
    return month * 12.0


def employer_social_cost(formula: str, salary: float, p) -> float:
    """按成本模型类型计算雇主法定社保/税费部分（不含工资本薪与奖金）。"""
    if formula == "single_pct":            # 瑞典：总包% + 假期%
        return salary * (_pct(p.get("total_pct")) + _pct(p.get("holiday_pct")))
    if formula == "single_pct_cap":        # 荷兰：min(salary,SV-loon)×总包% + 假期8%
        base = min(salary, float(p.get("sv_loon_cap") or 0))
        return base * _pct(p.get("total_pct")) + salary * _pct(p.get("holiday_pct"))
    if formula == "multi_cap":             # 美国：多档上限
        return (min(salary, float(p.get("oasdi_cap") or salary)) * _pct(p.get("oasdi_pct"))
                + salary * _pct(p.get("medicare_pct"))
                + min(salary, float(p.get("futa_cap") or 0)) * _pct(p.get("futa_pct"))
                + min(salary, float(p.get("suta_cap") or salary)) * _pct(p.get("suta_pct"))
                + salary * _pct(p.get("wc_pct")))
    if formula == "clamp":                 # 中国上海/北京/外籍：五险+公积金（clamp 年度基数）
        annual = _clamp_annual(salary, p.get("monthly_floor"), p.get("monthly_cap"))
        return annual * _pct(p.get("soc_pct")) + annual * _pct(p.get("housing_pct"))
    if formula == "headcount":             # 丹麦：人头固定 + 假期%
        return float(p.get("total_fixed") or 0) + salary * (_pct(p.get("feriepenge_pct")) + _pct(p.get("ferietillaeg_pct")))
    if formula == "uk_nic":                # 英国：NIC 超额起征点 + 养老金 + 工伤
        above = max(0.0, salary - float(p.get("nic_threshold") or 0))
        return (above * _pct(p.get("nic_pct"))
                + salary * _pct(p.get("pension_pct"))
                + salary * _pct(p.get("wc_pct")))
    if formula == "onss":                  # 比利时：ONSS% + 工伤 + 双倍假期92%×月 + 十三薪1×月
        return (salary * _pct(p.get("onss_pct"))
                + salary * _pct(p.get("wc_pct"))
                + salary / 12.0 * 0.92 + salary / 12.0 * 1.0)
    if formula == "ccss":                  # 卢森堡：CCSS%+工伤+MDE（注：5×SSM 工资上限待补）
        return salary * (_pct(p.get("ccss_pct")) + _pct(p.get("wc_pct")) + _pct(p.get("mde_pct")))
    if formula == "jp":                    # 日本：厚年/健保 clamp + 介护 + 雇佣+工伤（全）
        kosei_base = _clamp_annual(salary, None, p.get("kosei_cap"))
        kenpo_base = _clamp_annual(salary, None, p.get("kenpo_cap"))
        return (kosei_base * _pct(p.get("kosei_pct"))
                + kenpo_base * _pct(p.get("kenpo_pct"))
                + kenpo_base * _pct(p.get("kaigo_pct"))
                + salary * _pct(p.get("koyo_pct"))
                + salary * _pct(p.get("rosa_pct")))
    return 0.0


# --------------------------------------------------------------------------
# 在岗月折算
# --------------------------------------------------------------------------
def _overlap_months(opening: str | None, closing: str | None, year: int) -> int:
    """岗位在 target 年的在营月数（含边界月）。opening 空 → 0（不计）。"""
    if not opening:
        return 0
    try:
        o = date.fromisoformat(opening)
    except ValueError:
        return 0
    start = max(o.year * 100 + o.month, year * 100 + 1)
    end = year * 100 + 12
    if closing:
        try:
            c = date.fromisoformat(closing)
            end = min(c.year * 100 + c.month, end)
        except ValueError:
            pass
    if start > end:
        return 0
    sy = start // 100
    sm = start % 100
    ey = end // 100
    em = end % 100
    return ey * 12 + em - (sy * 12 + sm) + 1


# --------------------------------------------------------------------------
# 逐岗位成本
# --------------------------------------------------------------------------
def compute_annual_salary(db: Session, pos: dict, year: int) -> float | None:
    """岗位当年 Gross 年薪（税前）；无法匹配/缺基准 → None。"""
    loc = _locate(pos)
    if loc is None:
        return None
    region, _office, _bm = loc
    if is_outsourced(pos):
        base = avg_salary(db, region, year)
        if base is None:
            return None
        factor = OUTSOURCE_FACTOR.get(pos.get("legal_category") or "", 1.0)
        return base * factor                            # 每年现查当年基准
    open_year = _year_of(pos.get("opening_date"))
    if open_year is None:
        return None
    base = investment_fin_salary(db, region, open_year)
    if base is None:
        return None
    salary = base * (1.0 + LEVEL_PCT.get(pos.get("level") or "", 0.0))
    for y in range(open_year + 1, year + 1):            # 逐年增幅；负则不变
        g = wage_growth_pct(db, region, y)
        if g > 0:
            salary *= (1.0 + g / 100.0)
    return salary


def compute_position_cost(db: Session, pos: dict, year: int) -> dict | None:
    """逐岗位年成本：{salary, tax, bonus, total, months, currency, region, office}"""
    loc = _locate(pos)
    if loc is None:
        return None
    region, office, bonus_months = loc
    salary = compute_annual_salary(db, pos, year)
    if salary is None:
        return None
    currency = None
    w = _wage_row(db, region, year)
    if w:
        currency = w.currency
    tax = 0.0
    if office:
        row = _tax_row(db, office, year)
        if row:
            tax = employer_social_cost(row.formula, salary, row.params or {})
    else:
        tax = None                                    # 无税率 office → 税费未知
    bonus = salary / 12.0 * bonus_months
    tax_part = tax or 0.0
    total = salary + tax_part + bonus
    months = _overlap_months(pos.get("opening_date"), pos.get("closing_date"), year)
    return {"salary": round(salary, 2), "tax": (round(tax, 2) if tax is not None else None),
            "bonus": round(bonus, 2), "total": round(total * months / 12.0, 2),
            "months": months, "currency": currency, "region": region, "office": office,
            "company_id": pos.get("company_id"), "company_name": pos.get("company_name"),
            "position": pos.get("position_name") or pos.get("number"),
            "level": pos.get("level"), "outsourced": is_outsourced(pos)}


# --------------------------------------------------------------------------
# 升职薪资（晋升链精确化待补，v1 用决定式）
# --------------------------------------------------------------------------
def promotion_salary(old_salary: float, levels_between: int) -> float:
    """晋升：新职基薪 = 老职 × (1 + 5%×跨级数)。"""
    return old_salary * (1.0 + PROMOTION_STEP_PCT * levels_between)


def _year_of(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(str(iso)[:4])
    except ValueError:
        return None


# --------------------------------------------------------------------------
# 加薪规则 payload（UI「加薪规则」屏）
# --------------------------------------------------------------------------
def rules_payload() -> dict:
    return {
        "level_adjust_pct": {k: round(v * 100, 0) for k, v in LEVEL_PCT.items()},
        "outsource_factor": {k: v for k, v in OUTSOURCE_FACTOR.items()},
        "outsource_base": "当年全行业人均名义年薪（基准）",
        "bonus_months_default": BONUS_MONTHS_DEFAULT,
        "bonus_months_japan": BONUS_MONTHS_JAPAN,
        "promotion_step_pct": round(PROMOTION_STEP_PCT * 100, 0),
        "cpi_rule": "每年按 CPI工资 对应 国家×年份 的「工资增幅%」调整税前工资；负增长则当年不涨薪",
        "base_rule": "基础 = 工作地点对应地区的「投资/金融行业年薪」(按岗位 opening 年份) × (1 + Level调整%)",
    }