"""通用文本工具（DESIGN §6.2）。

数字/货币/日期 解析，供各 parser 复用。日期默认规则(F)在 resolve_date 实现。
"""
from __future__ import annotations

import re
from datetime import date

# ---- 千分位 / 单位 / 约等 ----
_THOUSAND_RE = re.compile(r"[,\s]")
_WAN_RE = re.compile(r"(?<=\d)([万亿])(?![a-zA-Z])")
_APPROX_RE = re.compile(r"[≈~]?\s*")


def parse_number(raw: object, scale: float = 1.0) -> float | None:
    """把 '12,345' '1.2万' '≈4,047.30' 解析为 float；失败返回 None。

    scale 用于把源单位折成统一基底（如 万→×1, 无 special）。
    本函数**不做** 万/亿 自动乘基数，仅清洗文本；万以下照字面。
    """
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace(" ", "")
    s = _APPROX_RE.sub("", s)
    if not s or s in ("-", "--", "—", ""):
        return None
    try:
        return float(s) * scale
    except ValueError:
        return None


def parse_amount(raw: object, unit: str | None = None) -> float | None:
    """金额解析：去千分位、去单位后缀，返回数值（不含单位）。"""
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace(" ", "").replace("≈", "").replace("~", "")
    s = re.sub(r"(万|亿|USD|US|BEF|LUF|NLG|DKK|SEK|HKD|EUR|法郎|美元|元|万美金|万USD|万BEF)", "", s)
    if not s or s in ("-", "—"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---- 货币 ----
_CUR_RE = re.compile(r"(USD|US|BEF|LUF|NLG|DKK|SEK|HKD|EUR|EUR)", re.IGNORECASE)


def detect_currency(text: str) -> str | None:
    """从文本(或所在节标题)识别币种后缀；无则 None（由调用方继承节币种）。"""
    m = _CUR_RE.search(text or "")
    if not m:
        return None
    return m.group(1).upper() if m.group(1) not in ("US", "EUR") else {"US": "USD"}.get(m.group(1).upper(), "USD")


_CURRENCIES = ("BEF", "LUF", "NLG", "DKK", "SEK", "USD", "HKD", "EUR")


def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    from calendar import monthrange
    return date(year, month, monthrange(year, month)[1])


def resolve_date(year: int, month: int | None = None, day: int | None = None,
                 hint: str | None = None) -> date:
    """DESIGN §6.2 日期默认规则（F）：

    - 仅年份 -> 当年 12-30（注明「年初」-> 01-01）
    - 年+月 -> 该月月底（写明「月初」-> 月 1 日）
    - 上旬/中旬/下旬 -> 1 日 / 11 日 / 21 日
    hint 携带「年初」「月初」「上旬」等标注覆盖默认。
    """
    hint = (hint or "").strip()
    if "年初" in hint or hint in ("年初", "年首"):
        return date(year, 1, 1)
    if day is not None:
        return date(year, month or 12, day)
    if month is not None:
        if "月初" in hint or hint in ("月初", "月初"):
            return date(year, month, 1)
        if "上旬" in hint:
            return date(year, month, 1)
        if "中旬" in hint:
            return date(year, month, 11)
        if "下旬" in hint:
            return date(year, month, 21)
        return _month_end(year, month)
    return date(year, 12, 30)


def currency_from(seg_title: str) -> str | None:
    """从分节标题（如 `## 一、BEF（祖父）`）抽币种。"""
    for c in _CURRENCIES:
        if c in (seg_title or "").upper():
            return c
    return None


def parse_relationship(line: str) -> tuple[str, ...] | None:
    """从人物 `- 关系：xxx` 字段抽 rel_type + target（简化实现，后续扩展）。"""
    m = re.match(r"^关系[:：]\s*(.+)$", line.strip())
    if not m:
        return None
    return tuple(x.strip() for x in m.group(1).split("、") if x.strip())