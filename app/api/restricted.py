"""受限写通道（DESIGN §14.1）：仅供 importer/数据调整员，普通 UI 403。

除 timeline-events（覆盖层）、source-files versions（diff 决策）与
UI 派生通道（投资/划拨等 §19）外，其余写端点挂 `require_importer`。

实现注记（issue #87-4 闭环）：此前守卫空转，现挂载以下受限端点并补 403 测试：
  POST /entities, PUT/PATCH/DELETE /entities/{id},
  POST /ledger-entries, POST /finance-entries,
  POST /entities/{id}/relationships, DELETE /relationships/{id}

本地单机无鉴权，用 header X-Importer:1 放行。
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_importer
from app.model import Entity, FinanceEntry, LedgerEntry, Relationship

router = APIRouter(prefix="/api/v1", tags=["restricted-writes"])


# ---------- Entities ----------
class EntityCreate(BaseModel):
    entity_type: str
    name: str
    display_name: Optional[str] = None
    status: Optional[str] = None
    fields: Optional[dict] = None


class EntityPatch(BaseModel):
    display_name: Optional[str] = None
    status: Optional[str] = None
    fields: Optional[dict] = None


@router.post("/entities", status_code=201, dependencies=[Depends(require_importer)])
def create_entity(body: EntityCreate, db: Session = Depends(get_db)):
    if body.entity_type not in ("person", "company", "asset", "family"):
        raise HTTPException(status_code=422, detail="entity_type 非法")
    e = Entity(entity_type=body.entity_type, name=body.name, display_name=body.display_name,
               status=body.status, fields=body.fields or {}, source="file")
    db.add(e)
    try:
        db.commit()
        db.refresh(e)
    except Exception as ex:  # 唯一键冲突等
        db.rollback()
        raise HTTPException(status_code=409, detail=str(ex))
    return {"id": e.id, "type": e.entity_type, "name": e.name}


@router.put("/entities/{entity_id}", dependencies=[Depends(require_importer)])
def replace_entity(entity_id: int, body: EntityCreate, db: Session = Depends(get_db)):
    e = db.get(Entity, entity_id)
    if not e:
        raise HTTPException(status_code=404, detail="entity not found")
    e.entity_type = body.entity_type
    e.name = body.name
    e.display_name = body.display_name
    e.status = body.status
    if body.fields is not None:
        e.fields = body.fields
    db.commit()
    return {"id": e.id, "type": e.entity_type, "name": e.name}


@router.patch("/entities/{entity_id}", dependencies=[Depends(require_importer)])
def patch_entity(entity_id: int, body: EntityPatch, db: Session = Depends(get_db)):
    e = db.get(Entity, entity_id)
    if not e:
        raise HTTPException(status_code=404, detail="entity not found")
    if body.display_name is not None:
        e.display_name = body.display_name
    if body.status is not None:
        e.status = body.status
    if body.fields is not None:
        e.fields = {**(e.fields or {}), **body.fields}
    db.commit()
    return {"id": e.id, "type": e.entity_type, "name": e.name, "status": e.status}


@router.delete("/entities/{entity_id}", dependencies=[Depends(require_importer)])
def delete_entity(entity_id: int, db: Session = Depends(get_db)):
    """仅 --admin-clean 通道放行，普通流程 409（DESIGN §13.1）。"""
    # 普通流程拒绝删除（公司集合只增不减）；仅当环境变量 ALLOW_ADMIN_CLEAN=1 时放行
    if os.environ.get("ALLOW_ADMIN_CLEAN") != "1":
        raise HTTPException(status_code=409, detail="公司集合只增不减，普通流程禁止删除；需 --admin-clean 通道")
    e = db.get(Entity, entity_id)
    if not e:
        raise HTTPException(status_code=404, detail="entity not found")
    db.delete(e)
    db.commit()
    return {"deleted": entity_id}


# ---------- Relationships ----------
class RelCreate(BaseModel):
    to_entity_id: int
    rel_type: str
    since_year: Optional[int] = None
    until_year: Optional[int] = None


@router.post("/entities/{entity_id}/relationships", status_code=201, dependencies=[Depends(require_importer)])
def create_relationship(entity_id: int, body: RelCreate, db: Session = Depends(get_db)):
    if not db.get(Entity, entity_id) or not db.get(Entity, body.to_entity_id):
        raise HTTPException(status_code=404, detail="entity not found")
    r = Relationship(from_entity_id=entity_id, to_entity_id=body.to_entity_id,
                     rel_type=body.rel_type, since_year=body.since_year, until_year=body.until_year)
    db.add(r)
    db.commit()
    db.refresh(r)
    return {"id": r.id}


@router.delete("/relationships/{rel_id}", dependencies=[Depends(require_importer)])
def delete_relationship(rel_id: int, db: Session = Depends(get_db)):
    r = db.get(Relationship, rel_id)
    if not r:
        raise HTTPException(status_code=404, detail="relationship not found")
    db.delete(r)
    db.commit()
    return {"deleted": rel_id}


# ---------- Ledger ----------
class LedgerCreate(BaseModel):
    account_id: int
    date: str
    reason: Optional[str] = None
    inflow: Optional[float] = None
    outflow: Optional[float] = None
    balance: Optional[float] = None
    kind: Optional[str] = None
    note: Optional[str] = None


@router.post("/ledger-entries", status_code=201, dependencies=[Depends(require_importer)])
def create_ledger(body: LedgerCreate, db: Session = Depends(get_db)):
    from datetime import date as _date
    try:
        d = _date.fromisoformat(body.date)
    except ValueError:
        raise HTTPException(status_code=422, detail="date 需 YYYY-MM-DD")
    e = LedgerEntry(account_id=body.account_id, date=d, reason=body.reason,
                    inflow=body.inflow, outflow=body.outflow, balance=body.balance,
                    kind=body.kind, note=body.note)
    db.add(e)
    db.commit()
    db.refresh(e)
    return {"id": e.id}


# ---------- Finance ----------
class FinanceCreate(BaseModel):
    entity_id: int
    entity_kind: str
    year: int
    kind: str
    amount: Optional[float] = None
    currency: Optional[str] = None
    label: Optional[str] = None


@router.post("/finance-entries", status_code=201, dependencies=[Depends(require_importer)])
def create_finance(body: FinanceCreate, db: Session = Depends(get_db)):
    if body.entity_kind not in ("person", "company"):
        raise HTTPException(status_code=422, detail="entity_kind 仅 person/company")
    if body.kind not in ("income", "expense", "investment", "investment_income", "pool"):
        raise HTTPException(status_code=422, detail="kind 非法")
    if not db.get(Entity, body.entity_id):
        raise HTTPException(status_code=404, detail="entity not found")
    e = FinanceEntry(entity_id=body.entity_id, entity_kind=body.entity_kind, year=body.year,
                     kind=body.kind, amount=body.amount, currency=body.currency, label=body.label, source="file")
    db.add(e)
    db.commit()
    db.refresh(e)
    return {"id": e.id}
