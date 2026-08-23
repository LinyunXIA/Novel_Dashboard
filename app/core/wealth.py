"""财富曲线视图（DESIGN P0-2 / §7）：按账户×币种 + 全家族合计 + USD 展示折算。

账务本币记录（snapshot 已是本币），展示层按 exchange_rate 折 USD。

数值纪律（F-P0-? 修复 #2）：
- 汇率缺失时绝不静默 fallback 到 1.0；返回 None 让调用方扣出该币种
  并在响应里挂 missing_rates 显式告警（dev 库即此状态）。
- 反向分支 currency→USD 同样按基准常量（year IS NULL）回退，保持与正向分支对称。
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.model import ExchangeRate, Snapshot


def _usd_rate(session: Session, currency: str, year: int) -> float | None:
    """currency → USD 折算率（1 单位 currency = X USD）。

    返回 None 表示汇率缺失；调用方必须 1) 跳过该币种贡献，2) 记入 missing_rates。
    绝不静默返回 1.0 防止 BEF/DKK/NLG/SEK 裸加当美元（issue #2 根因）。
    """
    if currency == "USD":
        return 1.0
    # 正向：USD→<currency> 行，rate 是 1 USD 兑多少 currency；取倒数
    row = session.execute(
        select(ExchangeRate.rate).where(
            ExchangeRate.fx_from == "USD", ExchangeRate.fx_to == currency,
            or_(ExchangeRate.year == year, ExchangeRate.year.is_(None)),
        )
        # 具体年份优先于基准常量（NULL 排后）
        .order_by(ExchangeRate.year.is_(None), ExchangeRate.year.desc())
        .limit(1)
    ).first()
    if row is not None and row[0] is not None:
        return 1.0 / float(row[0])
    # 反向：<currency>→USD 行，按基准常量（year IS NULL）回退保持对称
    row2 = session.execute(
        select(ExchangeRate.rate).where(
            ExchangeRate.fx_from == currency, ExchangeRate.fx_to == "USD",
            or_(ExchangeRate.year == year, ExchangeRate.year.is_(None)),
        )
        .order_by(ExchangeRate.year.is_(None), ExchangeRate.year.desc())
        .limit(1)
    ).first()
    return float(row2[0]) if row2 is not None and row2[0] is not None else None


def family_total_usd(session: Session, year: int) -> dict:
    """该年全家族合计（各账户本币快照 → 折 USD 求和）。

    返回 {"family_total_usd": float, "missing_rates": [(currency, year)...]}
    汇率缺失时该币种不计入合计；missing_rates 给前端显式告警。
    """
    snaps = session.execute(
        select(Snapshot).where(Snapshot.as_of_year == year)
    ).scalars().all()
    tot = 0.0
    missing: set[tuple[str, int]] = set()
    for sn in snaps:
        cur = sn.currency or "USD"
        rate = _usd_rate(session, cur, year)
        if rate is None:
            missing.add((cur, year))
            continue
        tot += float(sn.value or 0.0) * rate
    return {"family_total_usd": round(tot, 2),
            "missing_rates": sorted(missing)}


def wealth_series(session: Session, year_from: int = 1947, year_to: int = 2025) -> dict:
    """逐年 {year: {family_total_usd, accounts, currencies, missing_rates}}。

    missing_rates 列出该年缺汇率的币种（去重），便于前端按需告警。
    """
    out: dict[int, dict] = {}
    for y in range(year_from, year_to + 1):
        snaps = session.execute(
            select(Snapshot).where(Snapshot.as_of_year == y)
        ).scalars().all()
        accounts: dict[str, float] = {}
        currencies: dict[str, float] = defaultdict(float)
        family = 0.0
        missing: set[str] = set()
        for sn in snaps:
            val = float(sn.value or 0.0)
            cur = sn.currency or "USD"
            accounts[sn.scope] = val
            currencies[cur] += val
            rate = _usd_rate(session, cur, y)
            if rate is None:
                missing.add(cur)
                continue
            family += val * rate
        out[y] = {"family_total_usd": round(family, 2),
                  "accounts": accounts,
                  "currencies": dict(currencies),
                  "missing_rates": sorted(missing)}
    return out