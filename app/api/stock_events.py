"""事件·股票 API（F-P2-02 · DESIGN §19.6）。

- GET   /stock-events/events         列导入的待关联/已关联事件（StockEvent，F-P2-02 导入产物）
- GET   /stock-events/positions      列当前持仓明细（holding_event open batches + 市值）
- POST  /stock-events/associate      把导入的 buy 事件关联到 entity+account → apply_buy（写 holding+ledger）
- POST  /stock-events/buy             手动买入（盒通道，写 holding batch + ledger 现金移出）
- POST  /stock-events/sell            手动卖出（FIFO，写 sell 行 + ledger 本金/盈亏）
- POST  /stock-events/dividend        分红（每股×现持仓 → ledger investment_income）
- POST  /stock-events/passive-uplift  被动抬升（仅 pseudo pct 行，不写 ledger）

写操作走 `_after_ui_write`（recompute + 快照 + recompute-done），保证 ledger 余额链与
持仓市值进总资产口径同步刷新（总资产 = 现金 + 专款池 + 股票持仓市值）。
"""
from __future__ import annotations

from datetime import date as _date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import apply_error, get_db
from app.config import CALENDAR_MAX_YEAR as _YEAR_MAX  # issue #141
from app.core.snapshot import rebuild_snapshots
from app.core.stock_cost import (
    apply_buy, apply_dividend, apply_passive_uplift, apply_sell,
)
from app.model import HoldingEvent, StockEvent

router = APIRouter(prefix="/api/v1", tags=["stock-events"])


class AssociateIn(BaseModel):
    stock_event_id: int
    entity_id: int
    account_id: int


class StockActionIn(BaseModel):
    entity_id: int
    company: str
    date: _date = Field(ge=_date(1947, 1, 1), le=_date(_YEAR_MAX, 12, 31))
    account_id: Optional[int] = None
    ticker: Optional[str] = None
    unit_price: Optional[float] = None
    shares: Optional[float] = None
    sell_price: Optional[float] = None
    per_share: Optional[float] = None
    pct: Optional[float] = None
    event_id: str = Field(min_length=3)


_apply_error = apply_error   # issue #132：收敛到 deps 共享


def _se_dict(se: StockEvent) -> dict:
    return {"id": se.id, "company": se.company, "ticker": se.ticker,
            "date": se.date.isoformat() if se.date else None,
            "event_type": se.event_type, "shares": float(se.shares) if se.shares is not None else None,
            "unit_price": float(se.unit_price) if se.unit_price is not None else None,
            "amount": float(se.amount) if se.amount is not None else None,
            "pct": float(se.pct) if se.pct is not None else None,
            "linked": se.linked_entity_id is not None}


@router.get("/stock-events/events")
def list_stock_events(linked: Optional[bool] = None, db: Session = Depends(get_db)):
    q = select(StockEvent).order_by(StockEvent.company, StockEvent.date, StockEvent.id)
    if linked is not None:
        q = q.where(StockEvent.linked_entity_id.isnot(None) if linked
                    else StockEvent.linked_entity_id.is_(None))
    rows = db.execute(q).scalars().all()
    return {"items": [_se_dict(se) for se in rows], "total": len(rows)}


@router.get("/stock-events/positions")
def list_positions(entity_id: Optional[int] = None, db: Session = Depends(get_db)):
    """当前持仓：按 (entity_id, company) 聚合 open batches，列均价/市值/最新占比标记。"""
    q = select(HoldingEvent).where(HoldingEvent.shares > 0,
                                HoldingEvent.event_type != "sell",
                                HoldingEvent.event_type != "pseudo",
                                HoldingEvent.closed_on.is_(None))
    if entity_id is not None:
        q = q.where(HoldingEvent.entity_id == entity_id)
    rows = db.execute(q.order_by(HoldingEvent.date, HoldingEvent.id)).scalars().all()
    groups: dict[tuple[int, str], list] = {}
    order: list[tuple[int, str]] = []
    for r in rows:
        key = (r.entity_id, r.company)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)
    out = []
    for key in order:
        eid, company = key
        grp = groups[key]
        total_shares = sum(float(r.shares) for r in grp)
        cost = sum(float(r.shares) * float(r.unit_price or 0.0) for r in grp)
        pct = None
        for r in sorted(grp, key=lambda x: x.date):
            if r.event_type == "pseudo" and r.pct is not None:
                pct = float(r.pct)
        out.append({
            "entity_id": eid, "company": company, "ticker": grp[0].ticker,
            "batches": len(grp), "total_shares": total_shares,
            "market_value": round(cost, 2), "pct": pct,
        })
    return {"items": out, "total": len(out)}


@router.post("/stock-events/associate")
def associate_stock_event(body: AssociateIn, db: Session = Depends(get_db)):
    """把导入的 buy 事件关联到 entity+account：apply_buy 实体化 holding batch + ledger 现金移出。"""
    se = db.get(StockEvent, body.stock_event_id)
    if not se:
        raise HTTPException(404, "stock event not found")
    if se.linked_entity_id is not None:
        return {"associated": True, "skipped": True, "account_id": se.linked_account_id}
    if se.event_type != "buy" or not se.shares or not se.unit_price:
        raise HTTPException(422, "仅 buy 事件可关联；sell/dividend 请用对应动作端点")
    try:
        r = apply_buy(db, entity_id=body.entity_id, company=se.company, ticker=se.ticker,
                      date=se.date, unit_price=float(se.unit_price), shares=float(se.shares),
                      event_id=f"se{se.id}", account_id=body.account_id)
    except ValueError as e:
        _apply_error(e)
    se.linked_entity_id = body.entity_id
    se.linked_account_id = body.account_id
    se.linked_at = datetime.now()
    if not r.get("skipped"):
        rebuild_snapshots(db, from_year=se.date.year)
        db.commit()
    return {"associated": True, "skipped": r.get("skipped", False),
            "account_id": body.account_id, "result": r}


def _settle(db, body: StockActionIn):
    """写操作后刷新快照（含持仓市值进 entity/family）。buy/sell/dividend 只动 ledger+holding，
    非杠杆投资账户，不需 recompute_all 的复利余额链；rebuild_snapshots 由 ledger 重算即可。"""
    rebuild_snapshots(db, from_year=body.date.year)
    db.commit()


@router.post("/stock-events/buy")
def post_buy(body: StockActionIn, db: Session = Depends(get_db)):
    try:
        r = apply_buy(db, entity_id=body.entity_id, company=body.company, ticker=body.ticker,
                      date=body.date, unit_price=body.unit_price, shares=body.shares,
                      event_id=body.event_id, account_id=body.account_id)
    except ValueError as e:
        _apply_error(e)
    if not r.get("skipped"):
        _settle(db, body)
    return {"skipped": r.get("skipped", False), "result": r}


@router.post("/stock-events/sell")
def post_sell(body: StockActionIn, db: Session = Depends(get_db)):
    try:
        r = apply_sell(db, entity_id=body.entity_id, company=body.company, date=body.date,
                       shares=body.shares, sell_price=body.sell_price,
                       event_id=body.event_id, account_id=body.account_id)
    except ValueError as e:
        _apply_error(e)
    if not r.get("skipped"):
        _settle(db, body)
    return {"skipped": r.get("skipped", False), "result": r}


@router.post("/stock-events/dividend")
def post_dividend(body: StockActionIn, db: Session = Depends(get_db)):
    try:
        r = apply_dividend(db, entity_id=body.entity_id, company=body.company, date=body.date,
                           per_share=body.per_share, event_id=body.event_id,
                           account_id=body.account_id)
    except ValueError as e:
        _apply_error(e)
    if not r.get("skipped"):
        _settle(db, body)
    return {"skipped": r.get("skipped", False), "result": r}


@router.post("/stock-events/passive-uplift")
def post_uplift(body: StockActionIn, db: Session = Depends(get_db)):
    try:
        r = apply_passive_uplift(db, entity_id=body.entity_id, company=body.company,
                                 date=body.date, to_pct=body.pct, event_id=body.event_id,
                                 ticker=body.ticker)
    except ValueError as e:
        _apply_error(e)
    # pseudo 不写 ledger、不涉余额/快照，无需重算
    return {"skipped": r.get("skipped", False), "result": r}