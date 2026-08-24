"""事件·股票：并购/分拆三形态成本随链引擎（F-P2-03 · DESIGN §19.6）。

三种形态（S19.6）：
1. 纯换股/分拆（split）按新股数占比摊成本、现金不动。
2. 换股+现金（cash_share）股票腿成本全随链、现金腿入余额且不冲减成本/不记损益。
3. 纯现金（cash）持仓归 0、现金入余额、不记损益。

纯函数输入归一化批次 list，可离线单测；apply_merger 做 DB 写入（holding_event 新批次
+ 结清旧行 + 现金 ledger）。事件 spec 见各函数 docstring。
"""
from __future__ import annotations

from datetime import date as _date
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.model import HoldingEvent, LedgerEntry

# ---------------------------------------------------------------------------
# 纯函数（批次 batch = {shares, unit_price[, batch_id]}）
# ---------------------------------------------------------------------------
def split_position(batches: list[dict], legs: list[dict]) -> list[dict]:
    """形态1 纯换股/分拆：旧批次成本按「新股份数占比」分摊到各新证券。

    legs = [{company, per_old_share}]（每持 1 旧股得 per_old_share 新股，如 UTC 1→1CARR/0.5OTIS）。
    保持 per-batch FIFO 粒度：每旧批对每腿生成一行。
    """
    per_sum = sum(leg["per_old_share"] for leg in legs) or 1.0
    result: list[dict] = []
    for bt in batches:
        cost_total = bt["shares"] * bt["unit_price"]
        for leg in legs:
            shares = bt["shares"] * leg["per_old_share"]
            cost = cost_total * leg["per_old_share"] / per_sum
            result.append({"company": leg["company"], "shares": shares,
                           "unit_price": cost / shares if shares else 0.0,
                           "from_batch": bt.get("batch_id")})
    return result


def cash_share_position(batches: list[dict], legs: list[dict], cash_per_share: float) -> tuple[list[dict], float]:
    """形态2 换股+现金：股票腿成本全额随链（多腿按股数占比）；现金腿=Σ旧股×cash_per_share。

    返回 (新批次, cash)。现金不冲减成本、不记损益。"""
    total_old = sum(b["shares"] for b in batches)
    cost_total = sum(b["shares"] * b["unit_price"] for b in batches)
    per_sum = sum(leg["per_old_share"] for leg in legs) or 1.0
    new: list[dict] = []
    for leg in legs:
        shares = total_old * leg["per_old_share"]
        cost = cost_total * leg["per_old_share"] / per_sum
        new.append({"company": leg["company"], "shares": shares,
                    "unit_price": cost / shares if shares else 0.0})
    cash = total_old * cash_per_share
    return new, cash


def cash_merger(batches: list[dict], cash_per_share: float) -> float:
    """形态3 纯现金：持仓归 0，仅返回现金 = Σ旧股 × cash_per_share。不记损益。"""
    return sum(b["shares"] for b in batches) * cash_per_share


# ---------------------------------------------------------------------------
# 事件 spec + DB 写入
# ---------------------------------------------------------------------------
# spec = {"entity_id":int, "date":"YYYY-MM-DD", "old_company":str,
#         "form":"split"|"cash_share"|"cash",
#         "legs":[{"company":..,"per_old_share":..}],
#         "cash_per_share":float, "cash_account_id":int|None}

def _open_batches(db: Session, entity_id: int, company: str) -> list[dict]:
    """读未结清（shares>0）的旧公司批次，依日期归并为批次。"""
    rows = db.execute(select(HoldingEvent).where(
        HoldingEvent.entity_id == entity_id,
        HoldingEvent.company == company,
        HoldingEvent.shares > 0,
    ).order_by(HoldingEvent.date, HoldingEvent.id)).scalars().all()
    return [{"shares": float(r.shares), "unit_price": float(r.unit_price or 0.0),
             "batch_id": r.batch_id} for r in rows]


def _next_batch_ids(db: Session, n: int) -> Iterator[int]:
    m = db.execute(select(HoldingEvent.batch_id).order_by(HoldingEvent.batch_id.desc()).limit(1)) \
        .scalar_one_or_none()
    start = (m or 0) + 1
    return iter(range(start, start + n))


def _already_applied(db: Session, spec: dict) -> bool:
    if not spec.get("legs"):
        return False
    first_company = spec["legs"][0]["company"]
    return db.execute(select(HoldingEvent.id).where(
        HoldingEvent.entity_id == spec["entity_id"],
        HoldingEvent.company == first_company,
        HoldingEvent.date == spec["date"],
    ).limit(1)).scalar_one_or_none() is not None


def apply_merger(db: Session, spec: dict, source: str | None = None) -> dict:
    """对持仓主体应用一次并购/分拆事件：写新批次 + 结清旧行 + 现金 ledger。返回 {new_batches, cash, closed}。"""
    form = spec.get("form")
    entity_id = spec["entity_id"]
    old_company = spec["old_company"]
    event_date = _date.fromisoformat(spec["date"])   # spec 日期字符串 → date 对象

    if _already_applied(db, spec):
        return {"new_batches": [], "cash": 0.0, "closed": 0, "skipped": True}

    batches = _open_batches(db, entity_id, old_company)
    new: list[dict] = []
    cash = 0.0
    closed = 0

    if batches:
        closed = len(batches)
        nid = _next_batch_ids(db, len(batches) * max(len(spec.get("legs") or []), 1) + (bool(cash)))
        if form == "split":
            new = split_position(batches, spec["legs"])
            evtype = "split"
        elif form == "cash_share":
            new, cash = cash_share_position(batches, spec["legs"], spec.get("cash_per_share") or 0.0)
            evtype = "acquire-share"
        elif form == "cash":
            cash = cash_merger(batches, spec.get("cash_per_share") or 0.0)
            evtype = "acquire-cash"
        else:
            raise ValueError(f"未知 form: {form}")

        # 写新批次
        for nb in new:
            db.add(HoldingEvent(
                entity_id=entity_id, company=nb["company"],
                ticker=next((l.get("ticker") for l in spec.get("legs") or [] if l["company"] == nb["company"]), None),
                date=event_date, event_type=evtype, batch_id=next(nid),
                shares=nb["shares"], unit_price=nb["unit_price"],
                amount=nb["shares"] * nb["unit_price"] / 10000.0,  # 万USD 口径
                source_file=source))
        # 结清旧行
        for r in db.execute(select(HoldingEvent).where(
                HoldingEvent.entity_id == entity_id,
                HoldingEvent.company == old_company,
                HoldingEvent.shares > 0)).scalars().all():
            r.shares = 0

        # 现金腿 → ledger
        if cash and spec.get("cash_account_id"):
            db.add(LedgerEntry(
                account_id=spec["cash_account_id"], date=event_date,
                reason=f"并购现金对价·{old_company}{'→'+','.join(l['company'] for l in spec.get('legs') or []) if spec.get('legs') else '退市'}",
                inflow=cash, balance=None, kind="investment_income", note=f"股权事件·{form}"))

    return {"new_batches": new, "cash": cash, "closed": closed, "skipped": False}