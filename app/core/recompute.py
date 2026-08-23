"""增量重算（DESIGN §9）：从受影响起点年向后重算，不全量。

recompute_account(session, account_id, from_year)：
- 重算该账户 from_year 起的余额链（ledger 逐行：后 = 前 + 入 − 出，按日期排序）
- 写回 ledger.balance；之后可供快照/曲线读取。

issue #28 修复：内部计算全程 Decimal（避免 float 二进制误差累积进账本）。
SQLAlchemy Numeric 列读出来本就是 Decimal，原代码用 float() 转换丢精度。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.model import LedgerEntry
from app.model.types import AccountStatus


def recompute_account(session: Session, account_id: int, from_year: int) -> dict:
    """从 from_year 起重算账户余额链（DESIGN §9.2 recompute_one）。

    关键约束：
    1. 基线只取 from_year 前最后一条分录的余额作为起算余额；from_year 起的所有行
       一律滚动重算（同年内不再「沿用已存在 balance 跳过」）。
    2. 不再有死代码覆盖：base 永远等于上一行的累计余额（首行则为 from_year 前最后
       一行的 balance，未取到则 0）。
    3. issue #28：内部计算全程 Decimal；e.balance / e.inflow / e.outflow 读出来已
       是 Decimal（SQLAlchemy Numeric 列），直接相加不再转 float。
    """
    entries = session.execute(
        select(LedgerEntry).where(LedgerEntry.account_id == account_id)
        .order_by(LedgerEntry.date, LedgerEntry.id)
    ).scalars().all()

    # 基线：from_year 前最后一条分录的余额；取不到则 0
    baseline: Decimal = Decimal(0)
    for e in entries:
        if e.date.year < from_year and e.balance is not None:
            baseline = Decimal(e.balance)
        elif e.date.year >= from_year:
            break

    balance: Decimal = baseline
    years_updated = 0
    for e in entries:
        if e.date.year < from_year:
            continue                              # 锁定基线之前的行
        inflow = Decimal(e.inflow) if e.inflow is not None else Decimal(0)
        outflow = Decimal(e.outflow) if e.outflow is not None else Decimal(0)
        balance = balance + inflow - outflow
        if e.balance is None or Decimal(e.balance) != balance:
            e.balance = balance
            years_updated += 1
    return {"account_id": account_id, "from_year": from_year,
            "entries": len(entries), "updated": years_updated}


def recompute_all(session: Session, from_year: int, reason: str = "manual") -> list[dict]:
    """全库增量重算（受影响起算年向后）。返回每账户结果。"""
    acc_ids = [a for a in session.execute(select(LedgerEntry.account_id).distinct()).scalars().all()]
    out = []
    for aid in acc_ids:
        out.append(recompute_account(session, aid, from_year))
    return out


def register_job(session: Session, start_year: int, reason: str, files: Optional[list] = None) -> int:
    """写入 recompute_job（DESIGN §9.3）并返回 job id。"""
    from app.model import RecomputeJob
    job = RecomputeJob(start_year=start_year, reason=reason, files=files or [],
                       status="done", created_at=date.today(), finished_at=date.today())
    session.add(job)
    session.flush()
    return int(job.id)


def record_recompute_done(session: Session, start_year: int, reason: str = "manual",
                          files: Optional[list] = None) -> dict:
    """DESIGN §9.2 步骤 3-4：写 recompute_job(status=done) → 建 recompute-done Notification。

    供 ingest/recompute 成功路径调用，UI 据此弹「全局重算完成」非阻断横幅（§9.3）。
    返回 {"job_id": int, "notification_id": int}；session.flush 后由外层 commit。
    """
    from app.model import Notification
    from datetime import datetime
    job_id = register_job(session, start_year, reason, files)
    notif = Notification(
        job_id=job_id,
        kind="recompute-done",
        title="全局重算完成",
        message=f"已在全局重算财富与派生数据（自 {start_year} 起）",
        payload={"start_year": start_year, "files": files or []},
        created_at=datetime.now(),
    )
    session.add(notif)
    session.flush()
    return {"job_id": job_id, "notification_id": int(notif.id)}