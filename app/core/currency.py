"""多币种折算（DESIGN §3 core/currency.py · issue #71 统一权威实现）。

usd_rate() 是全系统唯一的 currency→USD 折算入口（Decimal 口径）：
- 优先取该年具体汇率；缺则回退基准常量（year IS NULL）；
- 正向 USD→X 取倒数、反向 X→USD 直取，两分支对称；
- 直连缺失时两跳链式回退 X→Y→Z(Y≠X,Z) 连乘（issue #115：§7.1 链式口径；
  典型枢纽 EUR——2002 关池承接币种），闭合性由 H3 / conflict.check_fx_chain_closure 把关；
- **缺汇率返回 None**：调用方必须跳过该币种贡献并显式告警，
  绝不静默 fallback 1.0（issue #2 根因，数值纪律「宁缺勿错」不变）。

历史背景：snapshot.py / wealth.py 曾各持一份语义相同、精度不同的实现
（Decimal vs float），存在口径漂移风险；本模块合并后二者仅做薄适配。
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.model import ExchangeRate


def _load_pairs(session: Session) -> dict:
    """全量汇率载入内存：{(fx_from, fx_to): {year|None: Decimal}}。

    七轮审计 #186：rate 为 NULL/<=0 的行直接剔除——不再遮蔽同对 year=NULL
    基准常量（此前具体年 NULL 行会令 _direct_rate 返回 None 且不回退常量）。
    """
    rows = session.execute(
        select(ExchangeRate.fx_from, ExchangeRate.fx_to,
               ExchangeRate.year, ExchangeRate.rate)
    ).all()
    pairs: dict[tuple[str, str], dict] = {}
    for f, t, y, r in rows:
        if r is None:
            continue
        d = Decimal(str(r))
        if d <= 0:
            continue
        pairs.setdefault((f, t), {})[y] = d
    return pairs


def _direct_from(pairs: dict, f: str, t: str, year: int) -> Decimal | None:
    m = pairs.get((f, t))
    if not m:
        return None
    if year in m:
        return m[year]
    return m.get(None)


def _pair_from(pairs: dict, base: str, quote: str, year: int) -> Decimal | None:
    if base == quote:
        return Decimal(1)
    d = _direct_from(pairs, base, quote, year)
    if d is not None:
        return d
    inv = _direct_from(pairs, quote, base, year)
    if inv is not None:
        return Decimal(1) / inv
    return None


def rate_from_pairs(pairs: dict, currency: str, year: int) -> Decimal | None:
    """usd_rate 的纯函数核心（基于预载 pairs）——供批量路径零点查复用。"""
    if currency == "USD":
        return Decimal(1)
    direct = _pair_from(pairs, currency, "USD", year)
    if direct is not None:
        return direct
    for hub in _HUB_CURRENCIES:
        if hub == currency:
            continue
        leg1 = _pair_from(pairs, currency, hub, year)
        if leg1 is None:
            continue
        leg2 = _pair_from(pairs, hub, "USD", year)
        if leg2 is None:
            continue
        return (leg1 * leg2).quantize(Decimal("0.000001"))
    return None


def rate_loader(session: Session):
    """批量预载汇率 → 返回与 usd_rate 同语义的 (currency, year)->Decimal|None 闭包。

    七轮审计 #186 性能形态修复：wealth_series / rebuild_snapshots 循环内改用本闭包，
    消除每年×每币种的 usd_rate 点查 N+1（~800-3200 SELECT/次操作 → 单次全载）。
    """
    pairs = _load_pairs(session)

    def rate(currency: str, year: int) -> Decimal | None:
        return rate_from_pairs(pairs, currency, year)

    return rate


# 链式回退允许的枢纽币（issue #115）：EUR 为 2002 关池承接币、历史对全；
# USD 为表内天然中心。限制白名单防任意深度的意外组合。
_HUB_CURRENCIES = ("EUR",)


def usd_rate(session: Session, currency: str, year: int) -> Decimal | None:
    """1 单位 currency 兑多少 USD；汇率缺失返回 None。

    解析顺序：直连（X→USD / USD→X 倒数）→ 两跳链式 X→hub→USD 连乘。
    链式积按 1e-6 定点（与杠杆率精度同级）；任一腿缺失仍返回 None
    （宁缺勿错纪律不变）。
    """
    if currency == "USD":
        return Decimal(1)
    return rate_from_pairs(_load_pairs(session), currency, year)
