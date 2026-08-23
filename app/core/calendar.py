"""全局日历游标（DESIGN §8 / E）：as_of_date → 截至该日状态。

快照按年（as_of_year）预计算，日历游标把所选 as_of_date 归到其所在年，
返回该年快照。源数据缺粒度由日期规则补全（normalize.resolve_date）。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.model import Snapshot


def resolve_as_of_year(as_of_date: date) -> int:
    """as_of_date 所在日历年（日历口径，12-30 结算）。"""
    return as_of_date.year


def snapshot_as_of(session: Session, as_of_date: date) -> list[dict]:
    """截至 as_of_date 的快照（全物种 scope）。"""
    year = resolve_as_of_year(as_of_date)
    snaps = session.execute(
        select(Snapshot).where(Snapshot.as_of_year == year, Snapshot.as_of_date.is_(None))
    ).scalars().all()
    return [{"scope": s.scope, "value": float(s.value) if s.value is not None else 0.0,
             "currency": s.currency, "year": s.as_of_year} for s in snaps]


def default_calendar_bounds() -> tuple[int, int]:
    """日历年范围（DESIGN：1947 最早 – 2026 最晚）。"""
    return 1947, 2026