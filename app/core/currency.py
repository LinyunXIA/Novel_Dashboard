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

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.model import ExchangeRate


def _direct_rate(session: Session, fx_from: str, fx_to: str, year: int) -> Decimal | None:
    """fx_from→fx_to 该年汇率（具体年 > 基准常量）；缺失 None。"""
    row = session.execute(
        select(ExchangeRate.rate).where(
            ExchangeRate.fx_from == fx_from, ExchangeRate.fx_to == fx_to,
            or_(ExchangeRate.year == year, ExchangeRate.year.is_(None)),
        )
        .order_by(ExchangeRate.year.is_(None), ExchangeRate.year.desc())
        .limit(1)
    ).first()
    if row is not None and row[0] is not None:
        return Decimal(str(row[0]))
    return None


def _pair_rate(session: Session, base: str, quote: str, year: int) -> Decimal | None:
    """1 base 兑多少 quote：直取 base→quote，缺则取 quote→base 的倒数。"""
    if base == quote:
        return Decimal(1)
    d = _direct_rate(session, base, quote, year)
    if d is not None:
        return d
    inv = _direct_rate(session, quote, base, year)
    if inv is not None:
        return Decimal(1) / inv
    return None


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
    direct = _pair_rate(session, currency, "USD", year)
    if direct is not None:
        return direct
    for hub in _HUB_CURRENCIES:
        if hub == currency:
            continue
        leg1 = _pair_rate(session, currency, hub, year)
        if leg1 is None:
            continue
        leg2 = _pair_rate(session, hub, "USD", year)
        if leg2 is None:
            continue
        return (leg1 * leg2).quantize(Decimal("0.000001"))
    return None
