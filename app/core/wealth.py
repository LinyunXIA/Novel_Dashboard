"""财富曲线视图（DESIGN P0-2 / §7）：按账户×币种 + 全家族合计 + USD 展示折算。

账务本币记录（snapshot 已是本币），展示层按 exchange_rate 折 USD。
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.model import Account, Entity, ExchangeRate, Snapshot


def _usd_rate(session: Session, currency: str, year: int) -> float:
    """返回 `1 USD = X <currency>`（即 currency 每单位折多少 USD 的倒数用）；
    优先 CNY/USD 直接：这里计算 currency→USD = 1 / (USD→currency)。"""
    if currency == "USD":
        return 1.0
    r = session.execute(
        select(ExchangeRate.rate).where(
            ExchangeRate.fx_from == currency, ExchangeRate.fx_to == "USD",
            ExchangeRate.year == year or ExchangeRate.year.is_(None))
        .order_by(ExchangeRate.year.is_(None))
    ).scalar_one_or_none()
    if r is not None:
        return float(r)
    # 反向 USD→currency，取倒数
    r2 = session.execute(
        select(ExchangeRate.rate).where(
            ExchangeRate.fx_from == "USD", ExchangeRate.fx_to == currency,
            ExchangeRate.year == year)
    ).first()
    return 1.0 / float(r2[0]) if r2 else 1.0


def family_total_usd(session: Session, year: int) -> float:
    """该年全家族合计（各账户本币快照 → 折 USD 求和）。"""
    snaps = session.execute(
        select(Snapshot).where(Snapshot.as_of_year == year)
    ).scalars().all()
    tot = 0.0
    for sn in snaps:
        cur = sn.currency or "USD"
        tot += float(sn.value or 0.0) * _usd_rate(session, cur, year)
    return tot


def wealth_series(session: Session, year_from: int = 1947, year_to: int = 2025) -> dict:
    """逐年 {year: {family_total_usd, accounts: {scope: value}, currencies: {cur: total_raw}}}"""
    out: dict[int, dict] = {}
    for y in range(year_from, year_to + 1):
        snaps = session.execute(
            select(Snapshot).where(Snapshot.as_of_year == y)
        ).scalars().all()
        accounts: dict[str, float] = {}
        currencies: dict[str, float] = defaultdict(float)
        family = 0.0
        for sn in snaps:
            val = float(sn.value or 0.0)
            accounts[sn.scope] = val
            currencies[sn.currency or "USD"] += val
            family += val * _usd_rate(session, sn.currency or "USD", y)
        out[y] = {"family_total_usd": round(family, 2),
                  "accounts": accounts,
                  "currencies": dict(currencies)}
    return out