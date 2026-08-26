"""增量重算（DESIGN §9）：从受影响起点年向后重算，不全量。

recompute_account(session, account_id, from_year)：
- 重算该账户 from_year 起的余额链（含杠杆复利：balance_y = balance_{y-1}*(1+rate)+净流入）
- 写回 ledger.balance；之后可供快照/曲线读取。

issue #28 修复：内部计算全程 Decimal（避免 float 二进制误差累积进账本）。
杠杆/收益曲线计算委托 leverage.py（DESIGN §7.2），提供 recompute_one 供增量复用。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.leverage import recompute_one as _recompute_one
from app.model import LedgerEntry
from app.model.types import AccountStatus


def recompute_account(session: Session, account_id: int, from_year: int) -> dict:
    """从 from_year 起重算账户余额链（DESIGN §9.2 recompute_one）。

    委托 leverage.recompute_one 实现（含杠杆复利滚动）。
    """
    return _recompute_one(session, account_id, from_year)


def recompute_all(session: Session, from_year: int, reason: str = "manual") -> list[dict]:
    """全库增量重算（受影响起算年向后）。返回每账户结果。"""
    acc_ids = [a for a in session.execute(select(LedgerEntry.account_id).distinct()).scalars().all()]
    out = []
    for aid in acc_ids:
        out.append(recompute_account(session, aid, from_year))
    return out


def register_job(session: Session, start_year: int, reason: str, files: Optional[list] = None) -> int:
    """写入 recompute_job（DESIGN §9.3）并返回 job id。

    issue #72：created_at/finished_at 用 datetime.now()（列是 timestamptz，
    原 date.today() 依赖驱动隐式转换）。
    """
    from app.model import RecomputeJob
    now = datetime.now()
    job = RecomputeJob(start_year=start_year, reason=reason, files=files or [],
                       status="done", created_at=now, finished_at=now)
    session.add(job)
    session.flush()
    return int(job.id)


def record_recompute_done(session: Session, start_year: int, reason: str = "manual",
                          files: Optional[list] = None,
                          job_row=None) -> dict:
    """DESIGN §9.2 步骤 3-4：写 recompute_job(status=done) → 建 recompute-done Notification。

    §9.2d（issue #120/#140）：重算后按受影响起点（start_year）跑范围化健康复核——
    payload 携带：
    - health：H1-H5/H-STOCK 计数（from_year=start_year 范围口径）；
    - health_findings：crit 优先的前 N 条明细（「查看影响」入口的数据源）；
    - health_findings_total / health_error（校验自身异常时不再静默吞掉）。

    job_row（issue #138 异步通道）：传入已存在的 pending/running 任务行则复用它
    （收尾置 done），否则按旧路径新建 done 行。CLI 同步路径不受影响。
    返回 {"job_id": int, "notification_id": int}；session.flush 后由外层 commit。
    """
    from app.core import health
    from app.model import Notification, RecomputeJob
    if job_row is not None:
        job = job_row
    else:
        job_id_ = register_job(session, start_year, reason, files)
        job = session.get(RecomputeJob, job_id_)
    payload: dict = {"start_year": start_year, "files": files or [], "health": {}}
    try:
        report = health.run_report(session, from_year=start_year)
        summary: dict[str, dict] = {rule: {"total": 0, "warn": 0, "crit": 0}
                                    for rule in ("H1", "H2", "H3", "H4", "H5", "H-STOCK")}
        for r in report:
            s = summary.setdefault(r["rule"], {"total": 0, "warn": 0, "crit": 0})
            s["total"] += 1
            s[r["level"]] = s.get(r["level"], 0) + 1
        payload["health"] = {k: v.get("total", 0) for k, v in summary.items()}
        crit_first = sorted(report, key=lambda x: 0 if x["level"] == "crit" else 1)
        payload["health_findings"] = crit_first[:20]
        payload["health_findings_total"] = len(report)
    except Exception as e:  # noqa: BLE001 — 健康复核失败不阻断重算完成，但必须显式留痕
        payload["health_error"] = f"{type(e).__name__}: {e}"[:500]
    notif = Notification(
        job_id=int(job.id),
        kind="recompute-done",
        title="全局重算完成",
        message=f"已在全局重算财富与派生数据（自 {start_year} 起）",
        payload=payload,
        created_at=datetime.now(),
    )
    session.add(notif)
    job.status = "done"
    job.finished_at = datetime.now()
    session.flush()
    return {"job_id": int(job.id), "notification_id": int(notif.id)}