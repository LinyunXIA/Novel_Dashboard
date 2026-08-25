"""股票持仓市值估值（F-P2-02 · DESIGN §19.6；F-P2-03 follow-up 结清窗口化）。

持仓市值 = Σ 在 as_of 时刻**未结清**的批次（open，shares>0 且 closed_on 未到或未过 as_of）
的 shares × unit_price（成本基准）。
并入总资产口径：总资产 = 银行现金 + 投资专款池 + 股票持仓市值（§19.6）。

口径纪律（与 ledger/快照对齐）：
- 返回 **美元元**（与 account:* / entity:* / family:total 快照 value 一致），不做 /10000。
- 只有写 `holding_event.amount`（万USD 列）时才除 10000 —— 见 stock_cost / apply_buy。
- open 判定 = `shares > 0 AND closed_on IS NULL OR closed_on > as_of`：
  - 分拆/并购用 `closed_on` 标记结清（`stock_cost.apply_merger`，保留 shares/unit_price 历史），
    因此 **重构前年份正确计入旧公司市值、重构后计入新公司**，历史不丢失。
  - `sell`/`pseudo` 行是历史/占比标记，恒排除（不构成 open）。

已知残余局限（须明示）：`apply_sell` 的**部分卖出**仍会递减原 buy 行 shares（非全事件流
重放），故部分卖出之前的年份 as-of 为近似；完整按事件流重放（买卖/并购逐事件回滚）留待
后续改造。全量卖出/分拆/并购已走 `closed_on`，历史不丢。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.model import HoldingEvent


def _open_window(as_of: date):
    """open 时间窗：closed_on IS NULL 或 晚于 as_of（分拆/并购前年份计入、之后排除）。"""
    return HoldingEvent.closed_on.is_(None) | (HoldingEvent.closed_on > as_of)


def market_value_at(session: Session, entity_id: int, as_of: date) -> float:
    """主体截至 as_of 的持仓成本市值（USD 元，非万）。

    口径：Σ open 批次（shares>0、date<=as_of、closed_on 窗口未过、排除 sell/pseudo）× unit_price。
    """
    rows = session.execute(
        select(HoldingEvent.shares, HoldingEvent.unit_price).where(
            HoldingEvent.entity_id == entity_id,
            HoldingEvent.shares > 0,
            HoldingEvent.event_type != "sell",
            HoldingEvent.event_type != "pseudo",
            HoldingEvent.date <= as_of,
            _open_window(as_of),
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
            _open_window(as_of),
        )
    ).all()
    out: dict[int, float] = {}
    for eid, sh, up in rows:
        out[int(eid)] = out.get(int(eid), 0.0) + float(sh) * float(up or 0.0)
    return out