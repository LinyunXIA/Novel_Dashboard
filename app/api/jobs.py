"""异步 job 资源 API（issue #138 · DESIGN §14.2 方案A）。

- POST/GET /api/v1/recompute-jobs、GET/DELETE /api/v1/recompute-jobs/{id}
- POST/GET /api/v1/import-jobs、GET/DELETE /api/v1/import-jobs/{id}

发起即建 job（202 Accepted），子操作 = 对该 job 的状态查询；后台执行由
FastAPI BackgroundTasks 承担（core/jobs 单 worker 串行）。DELETE 仅 pending
可取消（204），其余状态 409。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import CALENDAR_MIN_YEAR, get_config
from app.core import jobs
from app.model import ImportJob, RecomputeJob

router = APIRouter(prefix="/api/v1", tags=["jobs"])


class RecomputeJobIn(BaseModel):
    start_year: int | None = Field(default=CALENDAR_MIN_YEAR, ge=1947)
    reason: str | None = None
    files: list[str] | None = None


class ImportJobIn(BaseModel):
    provider: str
    payload: dict | None = None


def _recompute_dto(j: RecomputeJob) -> dict:
    return {"id": j.id, "start_year": j.start_year, "reason": j.reason,
            "files": j.files or [], "status": j.status,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None}


def _import_dto(j: ImportJob) -> dict:
    return {"id": j.id, "provider": j.provider, "payload": j.payload,
            "status": j.status, "result": j.result, "error": j.error,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None}


# ---------------- recompute-jobs ----------------
@router.post("/recompute-jobs", status_code=202)
def create_recompute_job(body: RecomputeJobIn, bg: BackgroundTasks,
                         db: Session = Depends(get_db)):
    """触发异步重算任务：建 pending → 后台执行 → GET 查询状态。"""
    jid = jobs.create_recompute_job(db, body.start_year or CALENDAR_MIN_YEAR,
                                    body.reason or "ui", body.files)
    db.commit()
    bg.add_task(jobs.execute_recompute_job, get_config().env, jid)
    return {"id": jid, "status": "pending"}


@router.get("/recompute-jobs")
def list_recompute_jobs(status: Optional[str] = None,
                        limit: int = Query(50, le=200), offset: int = Query(0, ge=0),
                        db: Session = Depends(get_db)):
    q = select(RecomputeJob).order_by(RecomputeJob.id.desc())
    if status:
        q = q.where(RecomputeJob.status == status)
    rows = db.execute(q.limit(limit).offset(offset)).scalars().all()
    # 四轮审计 #169：total=过滤后总数（此前为页内条数，破坏分页语义）
    total = db.execute(select(func.count()).select_from(RecomputeJob)
                       .where(*([RecomputeJob.status == status] if status else []))).scalar()
    return {"items": [_recompute_dto(x) for x in rows], "total": total}


@router.get("/recompute-jobs/{jid}")
def get_recompute_job(jid: int, db: Session = Depends(get_db)):
    j = db.get(RecomputeJob, jid)
    if j is None:
        raise HTTPException(status_code=404, detail="recompute job not found")
    dto = _recompute_dto(j)
    # 附最近一次通知的健康摘要/明细（「查看影响」数据源）
    from app.model import Notification
    n = db.execute(select(Notification)
                   .where(Notification.job_id == jid,
                          Notification.kind == "recompute-done")
                   .order_by(Notification.id.desc()).limit(1)).scalar_one_or_none()
    if n is not None:
        dto["health"] = (n.payload or {}).get("health")
        dto["health_findings"] = (n.payload or {}).get("health_findings")
        dto["health_error"] = (n.payload or {}).get("health_error")
    return dto


@router.delete("/recompute-jobs/{jid}", status_code=204)
def cancel_recompute_job(jid: int, db: Session = Depends(get_db)):
    if db.get(RecomputeJob, jid) is None:
        raise HTTPException(status_code=404, detail="recompute job not found")
    if not jobs.cancel_pending(db, RecomputeJob, jid):
        raise HTTPException(status_code=409, detail="仅 pending 任务可取消")


# ---------------- import-jobs ----------------
@router.post("/import-jobs", status_code=202)
def create_import_job(body: ImportJobIn, bg: BackgroundTasks,
                      db: Session = Depends(get_db)):
    """触发外部导入任务（provider ∈ {company-info, labor-cost}）。"""
    jid, err = jobs.create_import_job(db, body.provider, body.payload)
    if err:
        raise HTTPException(status_code=422, detail=err)
    db.commit()
    bg.add_task(jobs.execute_import_job, get_config().env, jid)
    return {"id": jid, "status": "pending", "provider": body.provider}


@router.get("/import-jobs")
def list_import_jobs(provider: Optional[str] = None, status: Optional[str] = None,
                     limit: int = Query(50, le=200), offset: int = Query(0, ge=0),
                     db: Session = Depends(get_db)):
    q = select(ImportJob).order_by(ImportJob.id.desc())
    conds = []
    if provider:
        q = q.where(ImportJob.provider == provider)
        conds.append(ImportJob.provider == provider)
    if status:
        q = q.where(ImportJob.status == status)
        conds.append(ImportJob.status == status)
    rows = db.execute(q.limit(limit).offset(offset)).scalars().all()
    # 四轮审计 #169：total=过滤后总数（此前为页内条数）
    total = db.execute(select(func.count()).select_from(ImportJob)
                       .where(*conds)).scalar()
    return {"items": [_import_dto(x) for x in rows], "total": total}


@router.get("/import-jobs/{jid}")
def get_import_job(jid: int, db: Session = Depends(get_db)):
    j = db.get(ImportJob, jid)
    if j is None:
        raise HTTPException(status_code=404, detail="import job not found")
    return _import_dto(j)


@router.delete("/import-jobs/{jid}", status_code=204)
def cancel_import_job(jid: int, db: Session = Depends(get_db)):
    if db.get(ImportJob, jid) is None:
        raise HTTPException(status_code=404, detail="import job not found")
    if not jobs.cancel_pending(db, ImportJob, jid):
        raise HTTPException(status_code=409, detail="仅 pending 任务可取消")
