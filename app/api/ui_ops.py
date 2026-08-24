"""UI 派生写通道：投资 / 划拨换汇（DESIGN §19 · F-P1-01/02/03/09）。

这些是前端可操作的改数据端点（走服务层校验 + 后传重算 + 编年史 overlay 同步），
与本依赖外其余受限写端点（需 X-Importer）区分开。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.invest import (
    create_investment, redeem_investment, region_start_years,
)
from app.core.recompute import recompute_all, record_recompute_done
from app.core.snapshot import rebuild_snapshots
from app.core.transfer import transfer
from app.model import Investment, InvestmentAlloc

router = APIRouter(prefix="/api/v1", tags=["ui-operations"])


class AllocIn(BaseModel):
    entity_id: int
    currency: str
    amount: Optional[float] = None
    is_all: bool = False


class InvestmentIn(BaseModel):
    year: int = Field(ge=1947, le=2026)
    region: str
    risk_lvl: str
    start_date: date
    allocs: list[AllocIn]


class TransferIn(BaseModel):
    source_account_id: int
    target_entity_id: int
    target_currency: str
    amount: float
    year: int = Field(ge=1947, le=2026)


def _apply_error(e: Exception):
    """投资/划拨业务错误 → HTTPException（422/409 per status）。"""
    status = getattr(e, "status", 422)
    raise HTTPException(status_code=status, detail=getattr(e, "detail", str(e)))


def _after_ui_write(session: Session, start_year: int, reason: str):
    """后传重算 + 快照 + 写 recompute-done notification（§9.2/§19.4 复用）。"""
    recompute_all(session, start_year)
    session.flush()
    rebuild_snapshots(session, from_year=start_year)
    record_recompute_done(session, start_year, reason)
    session.commit()


@router.get("/investments")
def list_investments(year: Optional[int] = None, region: Optional[str] = None,
                     db: Session = Depends(get_db)):
    q = select(Investment).order_by(Investment.year, Investment.id)
    if year is not None:
        q = q.where(Investment.year == year)
    if region:
        q = q.where(Investment.region == region)
    rows = db.execute(q).scalars().all()
    out = []
    for inv in rows:
        allocs = db.execute(
            select(InvestmentAlloc).where(InvestmentAlloc.investment_id == inv.id)
        ).scalars().all()
        out.append({
            "id": inv.id, "year": inv.year, "region": inv.region,
            "risk_lvl": inv.risk_lvl, "start_date": inv.start_date.isoformat(),
            "locked": inv.locked,
            "allocs": [{"entity_id": a.entity_id, "currency": a.currency,
                        "amount": float(a.amount), "is_all": a.is_all} for a in allocs],
        })
    return {"items": out, "total": len(out)}


@router.get("/investments/{investment_id}")
def get_investment(investment_id: int, db: Session = Depends(get_db)):
    inv = db.get(Investment, investment_id)
    if not inv:
        raise HTTPException(status_code=404, detail="investment not found")
    allocs = db.execute(
        select(InvestmentAlloc).where(InvestmentAlloc.investment_id == inv.id)
    ).scalars().all()
    return {
        "id": inv.id, "year": inv.year, "region": inv.region, "risk_lvl": inv.risk_lvl,
        "start_date": inv.start_date.isoformat(), "locked": inv.locked,
        "allocs": [{"entity_id": a.entity_id, "currency": a.currency,
                    "amount": float(a.amount), "is_all": a.is_all} for a in allocs],
    }


@router.post("/investments", status_code=201)
def post_investment(body: InvestmentIn, db: Session = Depends(get_db)):
    """创建投资（§19.1–19.3 校验链 + 划出走账 + 后传重算）。"""
    allocs = [{"entity_id": a.entity_id, "currency": a.currency,
               "amount": a.amount, "is_all": a.is_all} for a in body.allocs]
    try:
        inv = create_investment(db, year=body.year, region=body.region,
                                risk_lvl=body.risk_lvl, start_date=body.start_date,
                                allocs=allocs)
    except Exception as e:  # noqa: BLE001 —— 业务校验统一映射
        db.rollback()
        _apply_error(e)
    _after_ui_write(db, body.year, f"投资 {body.region} R{body.risk_lvl} {body.year}")
    return {"id": inv.id, "status": "created", "year": body.year, "region": body.region}


@router.post("/investments/{investment_id}/redeem")
def post_redeem(investment_id: int, db: Session = Depends(get_db)):
    """年末赎回（§19.2）：本金+收益划回银行、专款池清空。"""
    inv = db.get(Investment, investment_id)
    if not inv:
        raise HTTPException(status_code=404, detail="investment not found")
    try:
        out = redeem_investment(db, inv)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        _apply_error(e)
    _after_ui_write(db, inv.year, f"赎回投资 {inv.region} R{inv.risk_lvl} {inv.year}")
    return {"id": inv.id, **out}


@router.post("/transfers")
def post_transfer(body: TransferIn, db: Session = Depends(get_db)):
    """划拨（同币）/ 换汇（跨币）（§19.5）。"""
    try:
        out = transfer(db, source_account_id=body.source_account_id,
                       target_entity_id=body.target_entity_id,
                       target_currency=body.target_currency,
                       amount=body.amount, year=body.year)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        _apply_error(e)
    _after_ui_write(db, body.year, f"{out['operation']} {body.year}")
    return {"status": "ok", **out}


@router.get("/returns/regions")
def returns_regions():
    """地区起始年下限 + 收益国家映射（§19.1/§19.3，供前端下拉下限）。"""
    return region_start_years()