"""UI 派生写通道：投资 / 划拨换汇（DESIGN §19 · F-P1-01/02/03/09）。

这些是前端可操作的改数据端点（走服务层校验 + 后传重算 + 编年史 overlay 同步），
与本依赖外其余受限写端点（需 X-Importer）区分开。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import apply_error, get_db
from app.config import CALENDAR_MAX_YEAR as _YEAR_MAX  # issue #141
from app.core.demand import accrue_demand_interest
from app.core.invest import (
    InvestmentError, create_investment, redeem_investment, region_start_years,
    unlock_investment,
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
    year: int = Field(ge=1947, le=_YEAR_MAX)
    region: str
    risk_lvl: str
    start_date: date
    allocs: list[AllocIn]


class TransferIn(BaseModel):
    source_account_id: int
    target_entity_id: int
    target_currency: str
    amount: float
    year: int = Field(ge=1947, le=_YEAR_MAX)
    # 八轮审计 #189：客户端幂等键——同一表单重试复用同一 nonce → 服务端查重 skipped；
    # 缺省时服务端生成（兼容直调方，但该路径无重放保护）
    nonce: str | None = Field(default=None, max_length=32)


_apply_error = apply_error   # issue #132：收敛到 deps 共享


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
        out.append(_inv_dict(inv, allocs))
    return {"items": out, "total": len(out)}


def _inv_dict(inv, allocs) -> dict:
    """投资 → dict；expose redeemed（issue #82：前端置灰已赎回）。"""
    return {
        "id": inv.id, "year": inv.year, "region": inv.region,
        "risk_lvl": inv.risk_lvl, "start_date": inv.start_date.isoformat(),
        "locked": inv.locked, "redeemed": inv.redeemed_at is not None,
        "allocs": [{"entity_id": a.entity_id, "currency": a.currency,
                    "amount": float(a.amount), "is_all": a.is_all} for a in allocs],
    }


@router.get("/investments/{investment_id}")
def get_investment(investment_id: int, db: Session = Depends(get_db)):
    inv = db.get(Investment, investment_id)
    if not inv:
        raise HTTPException(status_code=404, detail="investment not found")
    allocs = db.execute(
        select(InvestmentAlloc).where(InvestmentAlloc.investment_id == inv.id)
    ).scalars().all()
    return _inv_dict(inv, allocs)


@router.post("/investments", status_code=201)
def post_investment(body: InvestmentIn, response: Response, db: Session = Depends(get_db)):
    """创建投资（§19.1–19.3 校验链 + 划出走账 + 后传重算）。"""
    allocs = [{"entity_id": a.entity_id, "currency": a.currency,
               "amount": a.amount, "is_all": a.is_all} for a in body.allocs]
    try:
        inv = create_investment(db, year=body.year, region=body.region,
                                risk_lvl=body.risk_lvl, start_date=body.start_date,
                                allocs=allocs)
    except InvestmentError as e:   # 业务校验族 → 422/409（五轮审计 #177：收窄捕获面）
        db.rollback()
        _apply_error(e)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="操作失败：服务内部错误，请查看服务日志")
    _after_ui_write(db, body.year, f"投资 {body.region} R{body.risk_lvl} {body.year}")
    response.headers["Location"] = f"/api/v1/investments/{inv.id}"   # §14.1（issue #127）
    return {"id": inv.id, "status": "created", "year": body.year, "region": body.region}


@router.post("/investments/{investment_id}/redeem")
def post_redeem(investment_id: int, db: Session = Depends(get_db)):
    """年末赎回（§19.2）：本金+收益划回银行、专款池清空。"""
    inv = db.get(Investment, investment_id)
    if not inv:
        raise HTTPException(status_code=404, detail="investment not found")
    try:
        out = redeem_investment(db, inv)
    except InvestmentError as e:   # 五轮审计 #177：业务族 → 422/409
        db.rollback()
        _apply_error(e)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="操作失败：服务内部错误，请查看服务日志")
    _after_ui_write(db, inv.year, f"赎回投资 {inv.region} R{inv.risk_lvl} {inv.year}")
    return {"id": inv.id, **out}


class InvestmentPatch(BaseModel):
    locked: bool = False


@router.patch("/investments/{investment_id}")
def patch_investment(investment_id: int, body: InvestmentPatch,
                     db: Session = Depends(get_db)):
    """§19.1 解锁重输（issue #81）：抹除本投资全部派生写入 + 置 locked=False。

    已赎回 → 409；解锁后重输走 POST /investments（unlocked 覆盖分支）。
    审计修复：请求重新锁定（locked=true）→ 422 明确拒绝（此前静默 no-op）——
    锁定只能由成功创建投资产生，不支持直接置回。
    """
    inv = db.get(Investment, investment_id)
    if not inv:
        raise HTTPException(status_code=404, detail="investment not found")
    if body.locked:
        raise HTTPException(
            status_code=422,
            detail="不支持重新锁定：如需改动请先解锁（locked=false）后整笔覆盖重输",
        )
    # 解锁（locked=false）：抹除旧写入，恢复 as-of
    try:
        inv = unlock_investment(db, inv)
    except InvestmentError as e:   # 五轮审计 #177：业务族 → 422/409
        db.rollback()
        _apply_error(e)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="操作失败：服务内部错误，请查看服务日志")
    _after_ui_write(db, inv.year, f"解锁投资 {inv.region} {inv.year}")
    return _inv_dict(inv, db.execute(
        select(InvestmentAlloc).where(InvestmentAlloc.investment_id == inv.id)
    ).scalars().all())


@router.post("/transfers")
def post_transfer(body: TransferIn, db: Session = Depends(get_db)):
    """划拨（同币）/ 换汇（跨币）（§19.5）。"""
    try:
        out = transfer(db, source_account_id=body.source_account_id,
                       target_entity_id=body.target_entity_id,
                       target_currency=body.target_currency,
                       amount=body.amount, year=body.year,
                       nonce=body.nonce or uuid4().hex[:12])   # 七轮 #182 + 八轮 #189：客户端幂等键透传
    except InvestmentError as e:   # 五轮审计 #177：业务族 → 422/409
        db.rollback()
        _apply_error(e)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="操作失败：服务内部错误，请查看服务日志")
    if out.get("skipped"):
        # 八轮审计 #189：幂等重放无写入——short-circuit，不跑全量重算/通知/commit
        return {"status": "skipped", **out}
    _after_ui_write(db, body.year, f"{out['operation']} {body.year}")
    return {"status": "ok", **out}


@router.get("/returns/regions")
def returns_regions():
    """地区起始年下限 + 收益国家映射（§19.1/§19.3，供前端下拉下限）。"""
    return region_start_years()


class DemandInterestIn(BaseModel):
    year: int = Field(ge=1947, le=_YEAR_MAX)


@router.post("/demand-interest")
def post_demand_interest(body: DemandInterestIn, db: Session = Depends(get_db)):
    """活期结息（§19.2 · 审计补齐）：未划拨资金 2% 年化按日折，12-30 入账。

    全部 active 账户逐日余额加权计息；同年重跑幂等覆盖（`demand#{year}` 标签）；
    未到结算日 → 422。成功后同请求内后传重算 + 快照 + recompute-done 通知。
    """
    try:
        out = accrue_demand_interest(db, body.year)
    except InvestmentError as e:   # 五轮审计 #177：业务族 → 422/409
        db.rollback()
        _apply_error(e)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="操作失败：服务内部错误，请查看服务日志")
    _after_ui_write(db, body.year, f"活期结息 {body.year}")
    return {"status": "ok", **out}