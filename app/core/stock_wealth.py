"""股票持仓市值估值（F-P2-02 · DESIGN §19.6）。

持仓市值 = Σ 未结清 opens（batch，shares>0）的 shares × unit_price（成本基准）。
并入总资产口径：总资产 = 银行现金 + 投资专款池 + 股票持仓市值（§19.6）。

口径纪律（与 ledger/快照对齐）：
- 返回 **美元元**（与 account:* / entity:* / family:total 快照 value 一致），不做 /10000。
- 只有写 `holding_event.amount`（万USD 列）时才除 10000 —— 见 stock_cost / apply_buy。
- `shares > 0` 即自动排除被 split/merge/sell 结清(closed) 的行（写时被置 0）。

已知局限（须明示）：`stock_cost.apply_merger` 破坏性把旧公司 shares 置 0（:142-146），
故分拆/并购**之前**的年份该上游公司市值会归零（新批次 date=重构日，date<=as_of 过滤只
在 >= 重构年生效）。净影响：>= 最后一次重构的年份完全正确，更早年份市值被漏。F-P2-02
的 `apply_sell` 采用非破坏式双写（减原 buy 行 + 自留 sell 行），是后续做全事件流 as-of
重放的最小支撑（完整修复 split 属 follow-up）。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.model import HoldingEvent


def market_value_at(session: Session, entity_id: int, as_of: date) -> float:
    """主体截至 as_of 的持仓成本市值（USD 元，非万）。

    口径：Σ HoldingEvent.shares>0 且 date<=as_of 的 shares×unit_price。
    """
    rows = session.execute(
        select(HoldingEvent.shares, HoldingEvent.unit_price).where(
            HoldingEvent.entity_id == entity_id,
            HoldingEvent.shares > 0,
            # sell/pseudo 是历史/占比标记，非 open 持仓（shares>0 只排除被减为 0 的）
            HoldingEvent.event_type != "sell",
            HoldingEvent.event_type != "pseudo",
            HoldingEvent.date <= as_of,
        )
    ).all()
    return float(sum(float(r.shares) * float(r.unit_price or 0.0) for r in rows))


def market_value_by_year(session: Session, entity_id: int, years: list[int]) -> dict[int, float]:
    """逐年 12-30 口径持仓市值（供 rebuild_snapshots / snapshot_as_of 批量）。"""
    return {y: market_value_at(session, entity_id, date(y, 12, 30)) for y in years}


def portfolio_breakdown(session: Session, as_of: date) -> dict[int, float]:
    """全实体截至 as_of 的持仓市值：{entity_id -> USD 元}。一次全库查，供快照/批量复用。"""
    rows = session.execute(
        select(HoldingEvent.entity_id, HoldingEvent.shares, HoldingEvent.unit_price).where(
            HoldingEvent.shares > 0,
            HoldingEvent.event_type != "sell",
            HoldingEvent.event_type != "pseudo",
            HoldingEvent.date <= as_of,
        )
    ).all()
    out: dict[int, float] = {}
    for eid, sh, up in rows:
        out[int(eid)] = out.get(int(eid), 0.0) + float(sh) * float(up or 0.0)
    return out