"""全局日历游标（DESIGN §8 / E）：as_of_date → 截至该日状态。

快照按年（as_of_year）预计算作默认年视图；日历游标对所选 as_of_date 采用
「按日即时累加」（issue #17 方案 A′）：截至所选日历日状态 = 固定值 + 累计到该日
的变更，直接对 ledger(date<=as_of_date) 求和，消除原「年中日期返回年末快照」的前视偏差。
源数据缺粒度由日期规则补全（normalize.resolve_date）。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.currency import usd_rate
from app.core.snapshot import account_balance_at
from app.model import Account


def resolve_as_of_year(as_of_date: date) -> int:
    """as_of_date 所在日历年（日历口径，12-30 结算）。"""
    return as_of_date.year


def snapshot_as_of(session: Session, as_of_date: date) -> list[dict]:
    """截至 as_of_date 的快照（account/entity/family 三层，按日累加）。

    口径与 rebuild_snapshots 一致，仅核对到 as_of_date 当日（而非某年年末）：
    - account  ：逐账户 balance=Σ(inflow−outflow) for date<=as_of_date，scope account:{id}:{cur}
    - entity   ：按 (entity_id, currency) 聚合，scope entity:{eid}:{cur}
    - family   ：复用 usd_rate 折算美元（汇率缺失币种不计入），scope family:total
    返回 dict 列表（含 year=as_of_date.year），与既有调用方（API /snapshots?as_of=、CLI calendar）兼容。
    """
    year = as_of_date.year
    accs = session.execute(select(Account)).scalars().all()
    out: list[dict] = []
    entity_agg: dict[tuple[int, str], float] = {}
    family_usd = 0.0
    for a in accs:
        bal = account_balance_at(session, a.id, as_of_date)
        # 关池后不双计：closed 账户自关池日起余额清 0（钱已结转进承接 EUR 账户，DESIGN §6.6）
        if a.status == "closed" and a.closed_on is not None and as_of_date >= a.closed_on:
            bal = 0.0
        out.append({"scope": f"account:{a.id}:{a.currency}",
                    "value": round(bal, 2), "currency": a.currency, "year": year})
        key = (a.entity_id, a.currency)
        entity_agg[key] = entity_agg.get(key, 0.0) + bal
        rate = usd_rate(session, a.currency, year)
        if rate is not None:
            family_usd += bal * float(rate)
    for (eid, cur), bal in entity_agg.items():
        out.append({"scope": f"entity:{eid}:{cur}",
                    "value": round(bal, 2), "currency": cur, "year": year})
    out.append({"scope": "family:total", "value": round(family_usd, 2),
                "currency": "USD", "year": year})
    return out


def default_calendar_bounds() -> tuple[int, int]:
    """日历年范围（DESIGN：1947 最早 – 2026 最晚）。"""
    return 1947, 2026