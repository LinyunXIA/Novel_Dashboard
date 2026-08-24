"""FastAPI 应用（F-P0-13）：基础只读路由 + 观测端 `/api/v1/` 前缀。

DESIGN §14：实体/账户/流水/快照/财富/收益/汇率/时间线；集合支持 as_of/分页。
本里程碑实现只读核心（写端点后续 P1 受限通道补）。

issue #23 修复：
- 抽 _paginated 公共分页；page>=1 校验，page_size 1..200
- /health 返回 summarize 结果（真 H1-H5）；liveness 另走 /ping
- 补 GET /accounts/{id} 详情
- 补 GET /timeline-events(+/id) 只读列表（编年史屏数据来源）
- snapshots 修 as_of 与 year 互斥语义
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.labor_cost import router as labor_cost_router
from app.api.ui_ops import router as ui_ops_router
from app.core.calendar import snapshot_as_of
from app.core.graph import all_graph, company_graph, person_graph
from app.core.health import run_report, summarize
from app.core.wealth import wealth_series
from app.model import (Account, Entity, ExchangeRate, FinanceEntry, IncomeStream,
                       LedgerEntry, Notification, ReturnCurve, Snapshot, TimelineEvent)

app = FastAPI(title="Novel Dashboard API", version="0.1")
app.include_router(ui_ops_router)
app.include_router(labor_cost_router)

# issue #30：dist 直连部署时前端跨域失败；PRD §13 本地单机非安全边界，
# 仅放行 vite dev server 默认端口（5173）的两个本地来源，不允许 * 通配。
_CORS_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_PREFIX = "/api/v1"


# ---------------- Liveness（与健康汇总分离；issue #23） ----------------
@app.get(API_PREFIX + "/ping", include_in_schema=True)
def _ping():
    """纯 liveness 探针：不连 DB、不查健康，给 k8s/容器探活用。"""
    return {"status": "ok"}


@app.get(API_PREFIX + "/health")
def health(db: Session = Depends(get_db)):
    """真 H1-H5 汇总（issue #23：原 /health 是冒牌 ping；现换为健康摘要）。

    返回 run_report 的完整问题清单 + 每规则计数。
    """
    return {"summary": summarize(db), "findings": run_report(db)}


# ---------------- 实体 ----------------
@app.get(API_PREFIX + "/entities")
def list_entities(
    type: Optional[str] = Query(None, description="person|company|asset|family"),
    page: int = Query(1, ge=1, description="页码，从 1 起"),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    q = select(Entity)
    if type:
        q = q.where(Entity.entity_type == type)
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar() or 0
    rows = db.execute(q.order_by(Entity.id).offset((page - 1) * page_size).limit(page_size)).scalars().all()
    return {
        "items": [{"id": e.id, "type": e.entity_type, "name": e.name, "status": e.status,
                   "display_name": e.display_name, "fields": e.fields} for e in rows],
        "total": total, "page": page, "page_size": page_size,
    }


@app.get(API_PREFIX + "/entities/{entity_id}")
def get_entity(entity_id: int, db: Session = Depends(get_db)):
    e = db.get(Entity, entity_id)
    if not e:
        raise HTTPException(status_code=404, detail="entity not found")
    return {"id": e.id, "type": e.entity_type, "name": e.name, "status": e.status,
            "fields": e.fields}


# ---------------- 账户 ----------------
@app.get(API_PREFIX + "/accounts")
def list_accounts(
    entity_id: Optional[int] = None,
    currency: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """issue #23：补分页（与 entities 对齐）。"""
    q = select(Account)
    if entity_id:
        q = q.where(Account.entity_id == entity_id)
    if currency:
        q = q.where(Account.currency == currency)
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar() or 0
    rows = db.execute(q.order_by(Account.id).offset((page - 1) * page_size).limit(page_size)).scalars().all()
    return {
        "items": [{"id": a.id, "entity_id": a.entity_id, "currency": a.currency,
                   "status": a.status, "closed_on": a.closed_on,
                   "migrate_to": a.migrate_to_currency} for a in rows],
        "total": total, "page": page, "page_size": page_size,
    }


@app.get(API_PREFIX + "/accounts/{account_id}")
def get_account(account_id: int, db: Session = Depends(get_db)):
    """issue #23：补账户详情（含开户行/状态/关池日）。"""
    a = db.get(Account, account_id)
    if not a:
        raise HTTPException(status_code=404, detail="account not found")
    return {
        "id": a.id, "entity_id": a.entity_id, "currency": a.currency,
        "status": a.status, "closed_on": a.closed_on,
        "migrate_to": a.migrate_to_currency, "bank": a.bank,
    }


@app.get(API_PREFIX + "/accounts/{account_id}/ledger-entries")
def account_ledger(
    account_id: int,
    year: Optional[int] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """issue #23：补分页。"""
    q = select(LedgerEntry).where(LedgerEntry.account_id == account_id)
    if year:
        q = q.where(func.extract("year", LedgerEntry.date) == year)
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar() or 0
    rows = db.execute(q.order_by(LedgerEntry.date, LedgerEntry.id)
                      .offset((page - 1) * page_size).limit(page_size)).scalars().all()
    return {
        "items": [{"id": e.id, "date": e.date.isoformat(), "reason": e.reason,
                   "inflow": float(e.inflow) if e.inflow is not None else None,
                   "outflow": float(e.outflow) if e.outflow is not None else None,
                   "balance": float(e.balance) if e.balance is not None else None,
                   "kind": e.kind} for e in rows],
        "total": total, "page": page, "page_size": page_size,
    }


# ---------------- 快照 / 日历游标 ----------------
@app.get(API_PREFIX + "/snapshots")
def snapshots(
    as_of: Optional[date] = None,
    year: Optional[int] = None,
    scope: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """issue #23：as_of 与 year 互斥（同时给 → 422），避免语义混叠静默忽略。"""
    if as_of and year:
        raise HTTPException(status_code=422, detail="as_of 与 year 不能同时指定")
    if as_of:
        snaps = snapshot_as_of(db, as_of)
        if scope:
            snaps = [s for s in snaps if s["scope"] == scope]
        return snaps
    q = select(Snapshot)
    if year:
        q = q.where(Snapshot.as_of_year == year)
    if scope:
        q = q.where(Snapshot.scope == scope)
    rows = db.execute(q).scalars().all()
    return [{"year": s.as_of_year, "scope": s.scope,
             "value": float(s.value) if s.value is not None else 0.0,
             "currency": s.currency} for s in rows]


# ---------------- 财富曲线 ----------------
@app.get(API_PREFIX + "/wealth")
def wealth(year_from: int = 1947, year_to: int = 2025, db: Session = Depends(get_db)):
    return wealth_series(db, year_from, year_to)


# ---------------- 收益曲线 ----------------
@app.get(API_PREFIX + "/returns/countries")
def returns_countries(db: Session = Depends(get_db)):
    """收益曲线在库国家列表（issue #87-3：前端动态渲染，不再硬编码 9 国）。"""
    rows = db.execute(
        select(ReturnCurve.country).where(ReturnCurve.country.isnot(None))
        .distinct().order_by(ReturnCurve.country)
    ).scalars().all()
    return {"countries": rows}


@app.get(API_PREFIX + "/returns")
def returns(
    country: Optional[str] = None,
    risk_lvl: Optional[str] = None,
    year: Optional[int] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """issue #23：补分页。"""
    q = select(ReturnCurve)
    if country:
        q = q.where(ReturnCurve.country == country)
    if risk_lvl:
        q = q.where(ReturnCurve.risk_lvl == risk_lvl)
    if year:
        q = q.where(ReturnCurve.year == year)
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar() or 0
    rows = db.execute(q.order_by(ReturnCurve.country, ReturnCurve.year)
                      .offset((page - 1) * page_size).limit(page_size)).scalars().all()
    return {
        "items": [{"country": r.country, "risk_lvl": r.risk_lvl, "year": r.year,
                   "rate": float(r.rate) if r.rate is not None else None} for r in rows],
        "total": total, "page": page, "page_size": page_size,
    }


# ---------------- 汇率 ----------------
@app.get(API_PREFIX + "/exchange-rates")
def fx(
    fx_from: Optional[str] = None,
    fx_to: Optional[str] = None,
    year: Optional[int] = None,                       # issue #87-1：按年筛可用方向
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """issue #23：补分页；issue #87-1：补 year 筛选（前端换汇目标币种可用方向）。"""
    q = select(ExchangeRate)
    if fx_from:
        q = q.where(ExchangeRate.fx_from == fx_from)
    if fx_to:
        q = q.where(ExchangeRate.fx_to == fx_to)
    if year:
        q = q.where(ExchangeRate.year == year)
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar() or 0
    rows = db.execute(q.order_by(ExchangeRate.fx_from, ExchangeRate.fx_to, ExchangeRate.year)
                      .offset((page - 1) * page_size).limit(page_size)).scalars().all()
    return {
        "items": [{"from": r.fx_from, "to": r.fx_to, "year": r.year,
                   "rate": float(r.rate) if r.rate else None} for r in rows],
        "total": total, "page": page, "page_size": page_size,
    }


# ---------------- 时间线（issue #23：编年史屏数据来源） ----------------
@app.get(API_PREFIX + "/timeline-events")
def list_timeline_events(
    year: Optional[int] = None,
    decade: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    q = select(TimelineEvent)
    if year is not None:
        q = q.where(TimelineEvent.event_year == year)
    if decade:
        q = q.where(TimelineEvent.decade == decade)
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar() or 0
    rows = db.execute(q.order_by(TimelineEvent.event_year, TimelineEvent.id)
                      .offset((page - 1) * page_size).limit(page_size)).scalars().all()
    return {
        "items": [{"id": t.id, "event_year": t.event_year,
                   "event_date": t.event_date.isoformat() if t.event_date else None,
                   "title": t.title, "note": t.note, "decade": t.decade,
                   "overlay": t.overlay} for t in rows],
        "total": total, "page": page, "page_size": page_size,
    }


@app.get(API_PREFIX + "/timeline-events/{event_id}")
def get_timeline_event(event_id: int, db: Session = Depends(get_db)):
    t = db.get(TimelineEvent, event_id)
    if not t:
        raise HTTPException(status_code=404, detail="timeline event not found")
    return {"id": t.id, "event_year": t.event_year,
            "event_date": t.event_date.isoformat() if t.event_date else None,
            "title": t.title, "note": t.note, "decade": t.decade,
            "overlay": t.overlay,
            "source_file": t.source_file, "source_line": t.source_line}


# ---------------- 财务收支（F-P1-07 · DESIGN §5 finance_entry） ----------------
@app.get(API_PREFIX + "/finance-entries")
def list_finance_entries(
    entity_id: Optional[int] = None,
    entity_kind: Optional[str] = None,
    kind: Optional[str] = None,
    year: Optional[int] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """财务收支列表（实体必填语义：所有行都挂 entity；过滤 entity/entity_kind/kind/year）。

    entity_kind 缺省时按 entity.entity_type 推导填充（asset/family 亦展示，来源仍合法）。
    """
    q = select(FinanceEntry)
    if entity_id:
        q = q.where(FinanceEntry.entity_id == entity_id)
    if entity_kind:
        q = q.where(FinanceEntry.entity_kind == entity_kind)
    if kind:
        q = q.where(FinanceEntry.kind == kind)
    if year:
        q = q.where(FinanceEntry.year == year)
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar() or 0
    rows = db.execute(q.order_by(FinanceEntry.year, FinanceEntry.id)
                      .offset((page - 1) * page_size).limit(page_size)).scalars().all()
    return {
        "items": [_fin2dict(r, db) for r in rows],
        "total": total, "page": page, "page_size": page_size,
    }


@app.get(API_PREFIX + "/entities/{entity_id}/finance-entries")
def entity_finance_entries(
    entity_id: int,
    kind: Optional[str] = None,
    year: Optional[int] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """以实体为中心的财务收支浏览（DESIGN §14 finance-entries; §5 实体必填）。"""
    e = db.get(Entity, entity_id)
    if not e:
        raise HTTPException(status_code=404, detail="entity not found")
    return list_finance_entries(entity_id=entity_id, kind=kind, year=year,
                                page=page, page_size=page_size, db=db)


def _fin2dict(f: FinanceEntry, db: Session) -> dict:
    # 用 relationship 惰性加载（同 session 内绑定），避免 db.get 遇 detached 实体报错
    ent = f.entity
    kind = f.entity_kind or (ent.entity_type if ent else None)
    return {
        "id": f.id, "entity_id": f.entity_id,
        "entity_name": (ent.display_name or ent.name) if ent else None,
        "entity_kind": kind, "year": f.year, "kind": f.kind,
        "amount": float(f.amount) if f.amount is not None else None,
        "currency": f.currency, "label": f.label, "source": f.source,
    }


# ---------------- 图谱（F-P1-04/05 只读 · DESIGN §14 graph） ----------------
@app.get(API_PREFIX + "/graph/persons")
def graph_persons(db: Session = Depends(get_db)):
    """人物图谱只读视图（人—人关系）。"""
    return person_graph(db)


@app.get(API_PREFIX + "/graph/companies")
def graph_companies(db: Session = Depends(get_db)):
    """公司图谱只读视图（公司—公司关系）。

    外部 API① 导入走 POST /graph/companies/import（F-P1-05；F-U7 UI 按钮触发）。
    """
    return company_graph(db)


@app.post(API_PREFIX + "/graph/companies/import")
def import_companies(db: Session = Depends(get_db)):
    """触发外部系统 API① 公司基础信息导入（DESIGN §13.1 · F-P1-05 / F-U7）。

    公司图谱页「获取/导入公司」按钮调用：拉取外部 /public/companies → 按只增不减
    upsert entity(company) + 股权关系 → commit → 返回统计 + 刷新后的公司图谱。
    本端点是 DESIGN §6.6/§13.1 明示的 UI 用户触发通道，故不挂 require_importer（§14.1
    注记不适用：此为 UI 派生动作）。网络/凭据失败 → 502/401，不落库。
    """
    from app.ingest.importers.company_info import run_external_company_import
    import httpx
    try:
        stats = run_external_company_import(db)
        db.commit()
    except httpx.HTTPStatusError as e:
        status = e.response.status_code if e.response is not None else 502
        raise HTTPException(status_code=status, detail=f"外部 API 请求失败（HTTP {status}）")
    except (httpx.RequestError, httpx.TimeoutException):
        raise HTTPException(status_code=502, detail="无法连接外部系统 API（请确认其已启动）")
    return {"stats": stats, "graph": company_graph(db)}


@app.get(API_PREFIX + "/graph/all")
def graph_all(db: Session = Depends(get_db)):
    """全量图谱（issue #84 / PRD §6.4 P1-1）：节点含 person/company/asset/family，边含跨类型。

    纯类型视图 /graph/persons、/graph/companies 保留不删；本端点供「人—公司」等跨类型关系
    可视化（前端按 entity_type 着色）。
    """
    return all_graph(db)


# ---------------- 通知（非阻断提示；DESIGN §9.3；issue #13） ----------------
@app.get(API_PREFIX + "/notifications")
def list_notifications(unread_only: bool = True, limit: int = Query(20, le=200),
                       db: Session = Depends(get_db)):
    """非阻断提示列表（重算完成/文件更新等）；默认仅未读，按创建倒序。"""
    q = select(Notification)
    if unread_only:
        q = q.where(Notification.read_at.is_(None))
    rows = db.execute(q.order_by(Notification.created_at.desc()).limit(limit)).scalars().all()
    return [{"id": n.id, "job_id": n.job_id, "kind": n.kind, "title": n.title,
             "message": n.message, "payload": n.payload,
             "read": n.read_at is not None,
             "created_at": n.created_at.isoformat() if n.created_at else None}
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
    n_ent = db.execute(select(func.count()).select_from(Entity)).scalar()
    n_acc = db.execute(select(func.count()).select_from(Account)).scalar()
    n_snap = db.execute(select(func.count()).select_from(Snapshot)).scalar()
    n_income = db.execute(select(func.count()).select_from(IncomeStream)).scalar()
    n_tl = db.execute(select(func.count()).select_from(TimelineEvent)).scalar()
    health = summarize(db)
    return {"entities": n_ent, "accounts": n_acc, "snapshots": n_snap,
            "income_streams": n_income, "timeline_events": n_tl, "health": health}