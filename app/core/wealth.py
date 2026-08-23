"""财富曲线视图（DESIGN P0-2 / §7）：按账户×币种 + 全家族合计 + USD 展示折算。

账务本币记录（snapshot 已是本币），展示层按 exchange_rate 折 USD。
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.model import ExchangeRate, Snapshot


def _usd_rate(session: Session, currency: str, year: int) -> float:
    """currency → USD 折算率（1 单位 currency = X USD）。

    汇率表存的是 `USD→<currency>`（1 USD 兑该币）方向，故取该方向的年汇率后取倒数。
    优先该具体年份；无则回退基准常量（year IS NULL）；再无可 1:1。
    """
    if currency == "USD":
        return 1.0
    row = session.execute(
        select(ExchangeRate.rate).where(
            ExchangeRate.fx_from == "USD", ExchangeRate.fx_to == currency,
            or_(ExchangeRate.year == year, ExchangeRate.year.is_(None)),
        )
        # 具体年份优先于基准常量
        .order_by(ExchangeRate.year.is_(None), ExchangeRate.year.desc())
        .limit(1)
    ).first()
    if row is not None and row[0] is not None:
        return 1.0 / float(row[0])
    # 反向：允许直接 currency→USD 行（若存在）
    row2 = session.execute(
        select(ExchangeRate.rate).where(
            ExchangeRate.fx_from == currency, ExchangeRate.fx_to == "USD",
            or_(ExchangeRate.year == year, ExchangeRate.year.is_(None)),
        )
        .order_by(ExchangeRate.year.is_(None), ExchangeRate.year.desc())
        .limit(1)
    ).first()
    return float(row2[0]) if row2 is not None and row2[0] is not None else 1.0


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