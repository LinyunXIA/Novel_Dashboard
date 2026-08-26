"""date-rule CRUD（DESIGN §6.2/§14.2 · issue #119）。

§6.2「超规则日期 → 用户补一条 date_rule → 后续解析复用」的登记通道。
规则语义：pattern=对原始日期格全文 re.search 的正则；resolve='MM-DD' 字面，
与 cell 内 4 位年份组合成日。变更后同步刷新 normalize 的进程内缓存。
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.ingest.normalize import load_date_rules
from app.model import DateRule

router = APIRouter(prefix="/api/v1/date-rules", tags=["date-rules"])

_MD_RE = re.compile(r"^\d{2}-\d{2}$")


class DateRuleIn(BaseModel):
    pattern: str = Field(min_length=1, description="正则，对原始日期格全文 search")
    resolve: str = Field(min_length=5, max_length=5, description="'MM-DD' 字面")
    note: str | None = None


def _reload(session: Session) -> None:
    rows = session.execute(select(DateRule.id, DateRule.pattern, DateRule.resolve)).all()
    load_date_rules(rows)


def _validate(body: DateRuleIn) -> None:
    try:
        re.compile(body.pattern)
    except re.error as e:
        raise HTTPException(status_code=422, detail=f"pattern 非法正则：{e}")
    if not _MD_RE.fullmatch(body.resolve):
        raise HTTPException(status_code=422, detail="resolve 需为 'MM-DD'（如 07-15）")


@router.get("")
def list_rules(db: Session = Depends(get_db)):
    rows = db.execute(select(DateRule).order_by(DateRule.id)).scalars().all()
    return {"items": [
        {"id": r.id, "pattern": r.pattern, "resolve": r.resolve,
         "note": r.note, "loaded": True} for r in rows], "total": len(rows)}


@router.post("", status_code=201)
def create_rule(body: DateRuleIn, response: Response,
                db: Session = Depends(get_db)):
    _validate(body)
    dup = db.execute(select(DateRule).where(DateRule.pattern == body.pattern)).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(status_code=409, detail=f"同 pattern 规则已存在 id={dup.id}")
    row = DateRule(pattern=body.pattern, resolve=body.resolve, note=body.note)
    db.add(row)
    db.commit()
    db.refresh(row)
    _reload(db)
    # issue #142：201 统一带 Location（#127 契约）
    if response is not None:
        response.headers["Location"] = f"/api/v1/date-rules/{row.id}"
    return {"id": row.id, "pattern": row.pattern, "resolve": row.resolve, "note": row.note}


@router.put("/{rule_id}")
def replace_rule(rule_id: int, body: DateRuleIn, db: Session = Depends(get_db)):
    _validate(body)
    row = db.get(DateRule, rule_id)
    if row is None:
        raise HTTPException(status_code=404, detail="date_rule not found")
    row.pattern = body.pattern
    row.resolve = body.resolve
    row.note = body.note
    db.commit()
    _reload(db)
    return {"id": row.id, "pattern": row.pattern, "resolve": row.resolve, "note": row.note}


@router.delete("/{rule_id}", status_code=204)
def delete_rule(rule_id: int, db: Session = Depends(get_db)):
    row = db.get(DateRule, rule_id)
    if row is None:
        raise HTTPException(status_code=404, detail="date_rule not found")
    db.delete(row)
    db.commit()
    _reload(db)
