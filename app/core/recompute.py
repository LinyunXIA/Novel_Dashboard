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
                          files: Optional[list] = None) -> dict:
    """DESIGN §9.2 步骤 3-4：写 recompute_job(status=done) → 建 recompute-done Notification。

    §9.2d（issue #120）：通知 payload 附带重算后的健康摘要（H1-H5/H-STOCK 计数），
    使「导入/UI 改动 → 重算 → 健康复核」链路自动闭环，无需手动跑 health。
    返回 {"job_id": int, "notification_id": int}；session.flush 后由外层 commit。
    """
    from app.core.health import summarize
    from app.model import Notification
    job_id = register_job(session, start_year, reason, files)
    try:
        health_summary = summarize(session)
    except Exception:  # noqa: BLE001 — 健康摘要失败不阻断重算完成通知
        health_summary = {}
    notif = Notification(
        job_id=job_id,
        kind="recompute-done",
        title="全局重算完成",
        message=f"已在全局重算财富与派生数据（自 {start_year} 起）",
        payload={"start_year": start_year, "files": files or [],
                 "health": {k: v.get("total", 0) for k, v in health_summary.items()}},
        created_at=datetime.now(),
    )
    session.add(notif)
    session.flush()
    return {"job_id": job_id, "notification_id": int(notif.id)}