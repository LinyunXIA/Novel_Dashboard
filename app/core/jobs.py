"""异步 job 执行器（issue #138 · DESIGN §14.2 方案A：补齐 job 资源）。

进程内单 worker（threading.Lock 串行化——本地单机语义）；每任务独立 session
（按 env 构造，测试可注入 sessionmaker）。

- recompute-jobs：create(pending) → 后台 running → recompute_all + rebuild_snapshots
  + record_recompute_done(job_row=本任务行，含范围化健康复核与通知) → done/failed。
- import-jobs：create(pending) → 后台 running → provider 分发：
  · company-info → run_external_company_import（外部 API① 公司基础信息）
  · labor-cost   → run_labor_cost（外部 API② 在岗岗位→用工成本落账；随后重算+快照）
  → done(result) / failed(error)。

状态机：pending → running → done | failed。DELETE 取消仅对 pending 有效（API 层）。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Callable

from app.config import CALENDAR_MIN_YEAR
from app.db import make_sessionmaker

PROVIDERS = ("company-info", "labor-cost")

# 单 worker 锁：本地单机同一时刻至多一个后台任务在执行；后续任务在锁上排队。
_job_lock = threading.Lock()


def create_recompute_job(session, start_year: int, reason: str = "ui",
                         files: list | None = None) -> int:
    """建 pending recompute_job，返回 id（§9.2 步骤1 的资源化）。"""
    from app.model import RecomputeJob
    job = RecomputeJob(start_year=start_year, reason=reason,
                       files=list(files or []), status="pending",
                       created_at=datetime.now())
    session.add(job)
    session.flush()
    return int(job.id)


def create_import_job(session, provider: str, payload: dict | None = None):
    """建 pending import_job；provider 非法返回 (None, 错误信息)。"""
    if provider not in PROVIDERS:
        return None, f"未知 provider {provider!r}，可选 {list(PROVIDERS)}"
    from app.model import ImportJob
    job = ImportJob(provider=provider, payload=dict(payload or {}), status="pending")
    session.add(job)
    session.flush()
    return int(job.id), None


def _default_runner(provider: str) -> Callable:
    """provider → runner(session, payload) -> result dict（复用既有同步内核）。"""
    if provider == "company-info":
        from app.ingest.importers.company_info import run_external_company_import

        def run(s, payload):
            stats = run_external_company_import(
                s, base_url=payload.get("base_url") or None)
            return {"stats": stats}
        return run
    if provider == "labor-cost":
        from app.ingest.importers.positions import run_labor_cost

        def run(s, payload):
            year = payload.get("year")
            if year is None:
                raise ValueError("labor-cost 需要 payload.year")
            return run_labor_cost(s, int(year),
                                  company_ids=payload.get("company_ids"))
        return run
    raise ValueError(f"未知 provider {provider!r}")


def _finish_ok(session, job, result: dict | None = None):
    job.status = "done"
    job.finished_at = datetime.now()
    if result is not None and hasattr(job, "result"):
        job.result = result


def _finish_failed(session, model, job_id, exc: Exception):
    # 独立事务收尾：业务回滚后仍要留下 failed 痕迹
    try:
        session.rollback()
        job = session.get(model, job_id)
        if job is not None:
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"[:500]
            job.finished_at = datetime.now()
            session.commit()
    except Exception:  # noqa: BLE001 — 收尾失败不再抛（避免污染后台线程日志）
        pass


def execute_recompute_job(env: str, job_id: int, *, sessionmaker=None) -> None:
    """后台执行重算任务（BackgroundTasks 入口；自管 session 与事务）。"""
    from app.config import calendar_years
    from app.core.recompute import recompute_all, record_recompute_done
    from app.core.snapshot import rebuild_snapshots
    from app.model import RecomputeJob

    S = sessionmaker or make_sessionmaker(env)
    with _job_lock:
        with S() as s:
            job = s.get(RecomputeJob, job_id)
            if job is None or job.status != "pending":
                return
            start_year = int(job.start_year or CALENDAR_MIN_YEAR)
            reason = job.reason or "recompute-job"
            files = list(job.files or [])
            job.status = "running"
            s.commit()
            try:
                recompute_all(s, start_year)
                rebuild_snapshots(s, calendar_years(), from_year=start_year)
                record_recompute_done(s, start_year, reason=reason, files=files,
                                      job_row=job)
                s.commit()
            except Exception as e:  # noqa: BLE001
                _finish_failed(s, RecomputeJob, job_id, e)


def execute_import_job(env: str, job_id: int, *, sessionmaker=None,
                       runners: dict[str, Callable] | None = None) -> None:
    """后台执行外部导入任务（provider 分发；labor 落账后同批重算+快照）。"""
    from app.config import calendar_years
    from app.core.recompute import recompute_all
    from app.core.snapshot import rebuild_snapshots
    from app.model import ImportJob

    S = sessionmaker or make_sessionmaker(env)
    with _job_lock:
        provider = None
        payload: dict = {}
        with S() as s:
            job = s.get(ImportJob, job_id)
            if job is None or job.status != "pending":
                return
            provider = job.provider
            payload = dict(job.payload or {})
            job.status = "running"
            s.commit()
        try:
            runner = (runners or {}).get(provider) or _default_runner(provider)
            with S() as s:
                result = runner(s, payload)
                if provider == "labor-cost":
                    year = int(payload.get("year") or CALENDAR_MIN_YEAR)
                    recompute_all(s, year)
                    rebuild_snapshots(s, calendar_years(), from_year=year)
                job = s.get(ImportJob, job_id)
                _finish_ok(s, job, result)
                s.commit()
        except Exception as e:  # noqa: BLE001 — 外部系统错误统一 failed 留痕（不透传上游码）
            with S() as s:
                _finish_failed(s, ImportJob, job_id, e)


def cancel_pending(session, model, job_id: int) -> bool:
    """取消 pending 任务（置 failed/已取消）；非 pending 返回 False（API→409）。"""
    job = session.get(model, job_id)
    if job is None or job.status != "pending":
        return False
    job.status = "failed"
    job.error = "已取消（DELETE pending 任务）"
    job.finished_at = datetime.now()
    session.commit()
    return True
