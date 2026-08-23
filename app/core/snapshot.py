"""逐年 as-of 快照预计算（DESIGN §8）。

口径：as_of_year 状态 = 固定值(初始) + 累计到该年的年标记变更（income − expense）。
对每个账户逐年归算余额，写入 snapshot(scope='account:{id}:{cur}')。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.model import Account, IncomeStream, LedgerEntry, Snapshot


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


def rebuild_snapshots(session: Session, years: range = range(1947, 2026)) -> dict:
    """重建全库逐年账户快照（scope='account:{id}:{cur}'）。"""
    # 清旧
    session.query(Snapshot).delete()
    stats = {"snapshots": 0, "accounts": 0}
    for acc in _ownership_accounts(session):
        series = _account_balance_series(session, acc.id, years)
        for y in years:
            session.add(Snapshot(
                as_of_year=y, as_of_date=None, scope=f"account:{acc.id}:{acc.currency}",
                value=series.get(y, 0.0), currency=acc.currency,
            ))
            stats["snapshots"] += 1
        stats["accounts"] += 1
    return stats