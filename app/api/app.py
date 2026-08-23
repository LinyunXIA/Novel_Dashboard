"""FastAPI 应用（F-P0-13）：基础只读路由 + 观测端 `/api/v1/` 前缀。

DESIGN §14：实体/账户/流水/快照/财富/收益/汇率；集合支持 as_of/分页。
本里程碑实现只读核心（写端点后续 P1 受限通道补）。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.calendar import snapshot_as_of
from app.core.health import run_report, summarize
from app.core.wealth import wealth_series
from app.model import (Account, Entity, ExchangeRate, IncomeStream, LedgerEntry,
                       Notification, ReturnCurve, Snapshot)

app = FastAPI(title="Novel Dashboard API", version="0.1")

API_PREFIX = "/api/v1"


@app.get(API_PREFIX + "/health", include_in_schema=False)
def _ping():
    return {"status": "ok"}


# ---------------- 实体 ----------------
@app.get(API_PREFIX + "/entities")
def list_entities(
    type: Optional[str] = Query(None, description="person|company|asset|family"),
    page: int = 1, page_size: int = Query(50, le=200),
    db: Session = Depends(get_db),
):
    q = select(Entity)
    if type:
        q = q.where(Entity.entity_type == type)
    rows = db.execute(q.order_by(Entity.id).offset((page - 1) * page_size).limit(page_size)).scalars().all()
    return [{"id": e.id, "type": e.entity_type, "name": e.name, "status": e.status,
             "display_name": e.display_name, "fields": e.fields} for e in rows]


@app.get(API_PREFIX + "/entities/{entity_id}")
def get_entity(entity_id: int, db: Session = Depends(get_db)):
    e = db.get(Entity, entity_id)
    if not e:
        raise HTTPException(status_code=404, detail="entity not found")
    return {"id": e.id, "type": e.entity_type, "name": e.name, "status": e.status,
            "fields": e.fields}


# ---------------- 账户 ----------------
@app.get(API_PREFIX + "/accounts")
def list_accounts(entity_id: Optional[int] = None, currency: Optional[str] = None,
                  db: Session = Depends(get_db)):
    q = select(Account)
    if entity_id:
        q = q.where(Account.entity_id == entity_id)
    if currency:
        q = q.where(Account.currency == currency)
    rows = db.execute(q).scalars().all()
    return [{"id": a.id, "entity_id": a.entity_id, "currency": a.currency,
             "status": a.status, "closed_on": a.closed_on, "migrate_to": a.migrate_to_currency} for a in rows]


@app.get(API_PREFIX + "/accounts/{account_id}/ledger-entries")
def account_ledger(account_id: int, year: Optional[int] = None, db: Session = Depends(get_db)):
    q = select(LedgerEntry).where(LedgerEntry.account_id == account_id)
    if year:
        q = q.where(func.extract("year", LedgerEntry.date) == year)
    rows = db.execute(q.order_by(LedgerEntry.date)).scalars().all()
    return [{"id": e.id, "date": e.date.isoformat(), "reason": e.reason,
             "inflow": float(e.inflow) if e.inflow is not None else None,
             "outflow": float(e.outflow) if e.outflow is not None else None,
             "balance": float(e.balance) if e.balance is not None else None,
             "kind": e.kind} for e in rows]


# ---------------- 快照 / 日历游标 ----------------
@app.get(API_PREFIX + "/snapshots")
def snapshots(as_of: Optional[date] = None, year: Optional[int] = None,
              scope: Optional[str] = None, db: Session = Depends(get_db)):
    q = select(Snapshot)
    if as_of:
        snaps = snapshot_as_of(db, as_of)
        if scope:
            snaps = [s for s in snaps if s["scope"] == scope]
        return snaps
    if year:
        q = q.where(Snapshot.as_of_year == year)
    if scope:
        q = q.where(Snapshot.scope == scope)
    rows = db.execute(q).scalars().all()
    return [{"year": s.as_of_year, "scope": s.scope, "value": float(s.value) if s.value is not None else 0.0,
             "currency": s.currency} for s in rows]


# ---------------- 财富曲线 ----------------
@app.get(API_PREFIX + "/wealth")
def wealth(year_from: int = 1947, year_to: int = 2025, db: Session = Depends(get_db)):
    return wealth_series(db, year_from, year_to)


# ---------------- 收益曲线 ----------------
@app.get(API_PREFIX + "/returns")
def returns(country: Optional[str] = None, risk_lvl: Optional[str] = None, year: Optional[int] = None,
            db: Session = Depends(get_db)):
    q = select(ReturnCurve)
    if country:
        q = q.where(ReturnCurve.country == country)
    if risk_lvl:
        q = q.where(ReturnCurve.risk_lvl == risk_lvl)
    if year:
        q = q.where(ReturnCurve.year == year)
    rows = db.execute(q.order_by(ReturnCurve.country, ReturnCurve.year)).scalars().all()
    return [{"country": r.country, "risk_lvl": r.risk_lvl, "year": r.year,
             "rate": float(r.rate) if r.rate is not None else None} for r in rows]


# ---------------- 汇率 ----------------
@app.get(API_PREFIX + "/exchange-rates")
def fx(fx_from: Optional[str] = None, fx_to: Optional[str] = None, db: Session = Depends(get_db)):
    q = select(ExchangeRate)
    if fx_from:
        q = q.where(ExchangeRate.fx_from == fx_from)
    if fx_to:
        q = q.where(ExchangeRate.fx_to == fx_to)
    rows = db.execute(q).scalars().all()
    return [{"from": r.fx_from, "to": r.fx_to, "year": r.year, "rate": float(r.rate) if r.rate else None} for r in rows]


# ---------------- 通知（非阻断提示；DESIGN §9.3；issue #13） ----------------
@app.get(API_PREFIX + "/notifications")
def list_notifications(unread_only: bool = True, limit: int = Query(20, le=200),
                       db: Session = Depends(get_db)):
    """非阻断提示列表（重算完成/文件更新等）；默认仅未读，按创建倒序。"""
    from datetime import datetime, timedelta
    q = select(Notification)
    if unread_only:
        q = q.where(Notification.read_at.is_(None))
    rows = db.execute(q.order_by(Notification.created_at.desc()).limit(limit)).scalars().all()
    return [{"id": n.id, "job_id": n.job_id, "kind": n.kind, "title": n.title,
             "message": n.message, "payload": n.payload,
             "read": n.read_at is not None, "created_at": n.created_at.isoformat() if n.created_at else None}
            for n in rows]


@app.patch(API_PREFIX + "/notifications/{notif_id}")
def mark_notification_read(notif_id: int, db: Session = Depends(get_db)):
    """标记通知已读（DESIGN API 表：PATCH /notifications/{id} → {read_at}）。"""
    from datetime import datetime
    n = db.get(Notification, notif_id)
    if not n:
        raise HTTPException(status_code=404, detail="notification not found")
    n.read_at = datetime.now()
    db.commit()
    return {"id": n.id, "read": True}


# ---------------- 总量概览 ----------------
@app.get(API_PREFIX + "/overview")
def overview(db: Session = Depends(get_db)):
    from app.model import IncomeStream, TimelineEvent
    n_ent = db.execute(select(func.count()).select_from(Entity)).scalar()
    n_acc = db.execute(select(func.count()).select_from(Account)).scalar()
    n_snap = db.execute(select(func.count()).select_from(Snapshot)).scalar()
    n_income = db.execute(select(func.count()).select_from(IncomeStream)).scalar()
    n_tl = db.execute(select(func.count()).select_from(TimelineEvent)).scalar()
    health = summarize(db)
    return {"entities": n_ent, "accounts": n_acc, "snapshots": n_snap,
            "income_streams": n_income, "timeline_events": n_tl, "health": health}