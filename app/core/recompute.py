"""增量重算（DESIGN §9）：从受影响起点年向后重算，不全量。

recompute_account(session, account_id, from_year)：
- 重算该账户 from_year 起的余额链（ledger 逐行：后 = 前 + 入 − 出，按日期排序）
- 写回 ledger.balance；之后可供快照/曲线读取。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.model import LedgerEntry
from app.model.types import AccountStatus


def recompute_account(session: Session, account_id: int, from_year: int) -> dict:
    """从 from_year 起重算账户余额链（DESIGN §9.2 recompute_one）。"""
    entries = session.execute(
        select(LedgerEntry).where(LedgerEntry.account_id == account_id)
        .order_by(LedgerEntry.date, LedgerEntry.id)
    ).scalars().all()
    balance = 0.0
    years_updated = 0
    for i, e in enumerate(entries):
        # 定位 from_year：之前年份的余额视为已定基线，从该年起重滚
        if e.date.year < from_year:
            balance = e.balance if e.balance is not None else balance
            continue
        if i > 0 and entries[i - 1].date.year == e.date.year and e.balance is not None:
            # 同年内已有基线则继续
            balance = e.balance if e.balance is not None else balance
            continue
        prev = entries[i - 1] if i > 0 else None
        base = (prev.balance if prev and prev.date.year >= from_year - 1 else 0.0)
        if e.date.year >= from_year or from_year == 0:
            base = balance
        inflow = float(e.inflow or 0.0)
        outflow = float(e.outflow or 0.0)
        balance = inflow - outflow + float(base)
        if e.balance != balance:
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