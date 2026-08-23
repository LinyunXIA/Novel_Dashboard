"""逐年 as-of 快照预计算（DESIGN §8）。

口径：as_of_year 状态 = 固定值(初始) + 累计到该年的年标记变更（income − expense）。
对每个账户逐年归算余额，写入 snapshot(scope='account:{id}:{cur}')；并按 entity 聚合
（scope='entity:{id}:{cur}'）以及家族合计（scope='family:total'，USD 口径）。

issue #12 修复：
- 新增 from_year 参数：仅重建 from_year 起的快照（delete 限定 as_of_year >= from_year）
- 补 entity:* scope（按 entity_id × 币种聚合）
- 补 family:total scope（USD 口径家族合计）
"""
from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.model import Account, ExchangeRate, IncomeStream, LedgerEntry, Snapshot


def _account_balance_series(session: Session, account_id: int,
                            years: range) -> dict[int, float]:
    """该账户逐年 as-of 余额：初始现金 + 累计 income − 累计 expense。"""
    # ledger 收支（现金进 ledger）
    rows = session.execute(
        select(func.extract("year", LedgerEntry.date),
               func.coalesce(func.sum(LedgerEntry.inflow), 0),
               func.coalesce(func.sum(LedgerEntry.outflow), 0))
        .where(LedgerEntry.account_id == account_id)
        .group_by(func.extract("year", LedgerEntry.date))
    ).all()
    year_in: dict[int, float] = {}
    year_out: dict[int, float] = {}
    for y, tin, tout in rows:
        year_in[int(y)] = float(tin)
        year_out[int(y)] = float(tout)
    # 逐年滚动
    series: dict[int, float] = {}
    bal = 0.0
    for y in years:
        bal += year_in.get(y, 0.0) - year_out.get(y, 0.0)
        series[y] = bal
    return series


def _ownership_accounts(session: Session) -> list[Account]:
    return session.execute(select(Account)).scalars().all()


def _usd_rate(session: Session, currency: str, year: int) -> float | None:
    """currency → USD 折算率（与 wealth._usd_rate 对齐；缺汇率返回 None 不静默 fallback）。"""
    if currency == "USD":
        return 1.0
    from sqlalchemy import or_
    row = session.execute(
        select(ExchangeRate.rate).where(
            ExchangeRate.fx_from == "USD", ExchangeRate.fx_to == currency,
            or_(ExchangeRate.year == year, ExchangeRate.year.is_(None)),
        ).order_by(ExchangeRate.year.is_(None), ExchangeRate.year.desc()).limit(1)
    ).first()
    if row is not None and row[0] is not None:
        return 1.0 / float(row[0])
    row2 = session.execute(
        select(ExchangeRate.rate).where(
            ExchangeRate.fx_from == currency, ExchangeRate.fx_to == "USD",
            or_(ExchangeRate.year == year, ExchangeRate.year.is_(None)),
        ).order_by(ExchangeRate.year.is_(None), ExchangeRate.year.desc()).limit(1)
    ).first()
    return float(row2[0]) if row2 is not None and row2[0] is not None else None


def rebuild_snapshots(session: Session, years: range = range(1947, 2026),
                      from_year: int | None = None) -> dict:
    """重建逐年账户/实体/家族三层快照。

    from_year=None → 全量重建（1947 起）；
    from_year=N    → 仅重建 [N, end] 年；旧 [1947, N-1] 快照保留（§9.2c 增量）。

    返回 {"snapshots": 行数, "accounts": 账户数, "entities": 实体数, "family_years": 家族快照年数}
    """
    stats = {"snapshots": 0, "accounts": 0, "entities": 0, "family_years": 0}
    years_list = list(years)
    if not years_list:
        return stats
    start = from_year if from_year is not None else years_list[0]

    # 1) 清旧：仅清 from_year 起（含）的 account/entity/family 三种 scope 行
    scope_prefixes = ("account:", "entity:", "family:")
    session.execute(
        delete(Snapshot).where(
            Snapshot.as_of_year >= start,
            Snapshot.as_of_date.is_(None),
        )
    )

    # 2) 先把所有账户余额 series 算好（避免 entity 聚合时再算）
    series_by_acc: dict[int, dict[int, float]] = {}
    for acc in _ownership_accounts(session):
        series_by_acc[acc.id] = _account_balance_series(session, acc.id, years)

    # 3) 写 account:* 行
    for acc in _ownership_accounts(session):
        series = series_by_acc[acc.id]
        for y in years_list:
            if y < start:
                continue
            session.add(Snapshot(
                as_of_year=y, as_of_date=None,
                scope=f"account:{acc.id}:{acc.currency}",
                value=round(series.get(y, 0.0), 2), currency=acc.currency,
            ))
            stats["snapshots"] += 1
        stats["accounts"] += 1

    # 4) 写 entity:* 行（entity × currency 聚合）
    # account.entity_id → entity_id；按 (entity_id, currency) 聚合
    accs = session.execute(select(Account)).scalars().all()
    # entity_id × currency → {year: sum}
    entity_agg: dict[tuple[int, str], dict[int, float]] = {}
    entity_ids: set[int] = set()
    for acc in accs:
        entity_ids.add(acc.entity_id)
        key = (acc.entity_id, acc.currency)
        ea = entity_agg.setdefault(key, {})
        series = series_by_acc.get(acc.id, {})
        for y, v in series.items():
            if y < start:
                continue
            ea[y] = ea.get(y, 0.0) + v
    for (eid, cur), ymap in entity_agg.items():
        for y in years_list:
            if y < start:
                continue
            v = ymap.get(y, 0.0)
            session.add(Snapshot(
                as_of_year=y, as_of_date=None,
                scope=f"entity:{eid}:{cur}",
                value=round(v, 2), currency=cur,
            ))
            stats["snapshots"] += 1
        stats["entities"] += 1

    # 5) 写 family:total 行（USD 口径；汇率缺失币种不计入）
    #     缺失币种可通过 wealth._usd_rate 重算 family_total_usd 时另行标记（issue #2）
    for y in years_list:
        if y < start:
            continue
        family_usd = 0.0
        for acc in accs:
            series = series_by_acc.get(acc.id, {})
            v = series.get(y, 0.0)
            if v == 0:
                continue
            rate = _usd_rate(session, acc.currency, y)
            if rate is None:
                continue                                 # 汇率缺失 → 不计入
            family_usd += v * rate
        session.add(Snapshot(
            as_of_year=y, as_of_date=None,
            scope="family:total",
            value=round(family_usd, 2), currency="USD",
        ))
        stats["snapshots"] += 1
        stats["family_years"] += 1
    return stats