"""地区/币种 → 收益曲线国家映射（单一权威定义，issue #113）。

源 canon 只有 5 份地区测算表（欧洲1947/英国1983/美国1989/香港1999/中国2002），
`return_curve.country` 存的就是这 5 个名字。历史上 REGION_COUNTRY 曾把
欧洲→比利时、香港→中国香港、中国→中国大陆（对应早期国别表设想），但库中
从无这些国家名 → `_rate_for_account_year` / `create_investment` 的收益查询
对真实数据恒为 None（投资 422、重算永不复利）。本模块固化为 identity 映射，
并集中币种→地区推断与 entity.fields 覆盖钩子的口径。
"""
from __future__ import annotations

# UI 地区 → return_curve.country（identity：源数据地区表即按此命名）
REGION_COUNTRY = {
    "欧洲": "欧洲",
    "英国": "英国",
    "美国": "美国",
    "香港": "香港",
    "中国": "中国",
}

# 区域起始年下限（DESIGN §19.3）：UI 选项下限 + serve 校验（422）
REGION_START_YEAR = {
    "欧洲": 1947,
    "英国": 1983,
    "美国": 1989,
    "香港": 1999,
    "中国": 2002,
}

# 币种 → 地区推断（账户无国家字段，按币种归属；EUR 承接自 BEF/LUF 归欧洲）：
#   祖父=BEF+LUF（比利时+卢森堡）、祖母=SEK、外祖父=NLG、外祖母=DKK
#   （CLAUDE.md 币种纪律）；GBP/HKD/CNY 对应其余地区。
CURRENCY_REGION = {
    "BEF": "欧洲",
    "LUF": "欧洲",
    "EUR": "欧洲",
    "SEK": "欧洲",
    "DKK": "欧洲",
    "NLG": "欧洲",
    "GBP": "英国",
    "USD": "美国",
    "HKD": "香港",
    "CNY": "中国",
}

# 默认风险等级（家族主仓口径；可被 entity.fields["risk_lvl"] 覆盖）
DEFAULT_RISK_LVL = "R3"


def currency_region(currency: str) -> str | None:
    """币种 → 地区；未知币种返回 None（调用方按缺收益率处理）。"""
    return CURRENCY_REGION.get(currency)


def entity_region_override(fields: dict | None) -> str | None:
    """entity.fields["return_region"] 覆盖币种推断（如公司主体挂美国曲线）。"""
    if fields and isinstance(fields.get("return_region"), str):
        return fields["return_region"]
    return None


def entity_risk_override(fields: dict | None) -> str | None:
    """entity.fields["risk_lvl"] 覆盖默认 R3（需 R1-R5 合法值）。"""
    if fields and fields.get("risk_lvl") in ("R1", "R2", "R3", "R4", "R5"):
        return fields["risk_lvl"]
    return None
