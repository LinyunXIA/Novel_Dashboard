"""时间线默认事件生成（Phase 2+ · F-P2+-04）。

`Design_Folder/时间线.md` 已清空为占位；时间线 `timeline_event` 改由本模块按导入数据自动生成
「默认事件」——只加**首次投资/发生**记录 + **每年 R1–R5 投资**，供「编年史」屏作初始时间轴。

生成源（读 DB；`SOURCE` 作 source_file 标记，`overlay=False`）：
1. **股票首次建仓**：`holding_event` 每 (entity, company) 最早一笔 `buy` →
   「{持有人}」首次建仓 {股票}
2. **影视首次投资**：`movie_event` 每部最早 `investment_date` → 投资《{电影}》
3. **股票事件首次**：`stock_event` 每 (company, event_type) 最早 → 「{公司}」首次 {事件}
4. **每年 R1–R5 投资**：`investment` 每行 → {年} 年 {region} R{risk} 投资（note 附 alloc 合计）

幂等：复用 `app.ingest.writer.import_timeline`（按 (event_year, title, source_file) 查重，存在即跳过）。
`rebuild=True` 先删本链路已生成的默认事件再重建（不触碰手工/overlay 行）。
"""
from __future__ import annotations

import datetime as _dt

from sqlalchemy import func, select

from app.model import Entity, HoldingEvent, Investment, InvestmentAlloc, MovieEvent, StockEvent, TimelineEvent

SOURCE = "derive:timeline-defaults"


def _day(d) -> _dt.date | None:
    """SQLAlchemy MIN(Date) 可能返回 date 或 datetime → 归一为 date；None 跳过。"""
    if d is None:
        return None
    if isinstance(d, _dt.datetime):
        return d.date()
    return d


def _mk(d: _dt.date, title: str, note: str | None = None) -> dict | None:
    if d is None or not title:
        return None
    year = d.year
    return {
        "event_year": year,
        "event_date": d,
        "title": title,
        "note": note,
        "decade": f"{(year // 10) * 10}s",
        "source_file": SOURCE,
    }


def _records(session) -> list[dict]:
    recs: list[dict] = []
    names = {e.id: e.name for e in session.execute(select(Entity)).scalars()}

    # 1. 股票首次建仓：per (entity, company) 最早 buy
    for entity_id, company, d in session.execute(
            select(HoldingEvent.entity_id, HoldingEvent.company,
                   func.min(HoldingEvent.date))
        .where(HoldingEvent.event_type == "buy")
        .group_by(HoldingEvent.entity_id, HoldingEvent.company)).all():
        rec = _mk(_day(d), f"「{names.get(entity_id, '?')}」首次建仓 {company}")
        if rec:
            recs.append(rec)

    # 2. 影视首次投资：per title 最早 investment_date
    for title, d in session.execute(
            select(MovieEvent.title, func.min(MovieEvent.investment_date))
        .where(MovieEvent.investment_date.isnot(None))
        .group_by(MovieEvent.title)).all():
        rec = _mk(_day(d), f"投资《{title}》")
        if rec:
            recs.append(rec)

    # 3. 股票事件首次：per (company, event_type) 最早
    for company, etype, d in session.execute(
            select(StockEvent.company, StockEvent.event_type,
                   func.min(StockEvent.date))
        .group_by(StockEvent.company, StockEvent.event_type)).all():
        rec = _mk(_day(d), f"「{company}」首次 {etype or '事件'}")
        if rec:
            recs.append(rec)

    # 4. 每年 R1–R5 投资：per investment 行，note 附 alloc 合计
    for inv in session.execute(select(Investment)).scalars():
        total = session.execute(
            select(func.coalesce(func.sum(InvestmentAlloc.amount), 0))
            .where(InvestmentAlloc.investment_id == inv.id)).scalar()
        note = f"投资额合计 {float(total or 0):,.2f}" if total else None
        rec = _mk(_day(inv.start_date), f"{inv.year} 年 {inv.region} {inv.risk_lvl} 投资", note)
        if rec:
            recs.append(rec)

    return recs


def derive_default_timeline(session, rebuild: bool = False, log=None) -> dict:
    """按导入数据生成时间线默认事件（幂等合并）。返回统计 dict。"""
    if rebuild:
        hits = session.execute(select(TimelineEvent.id).where(
            TimelineEvent.source_file == SOURCE)).scalars().all()
        n = 0
        for tid in hits:
            t = session.get(TimelineEvent, tid)
            if t is not None:
                session.delete(t)
                n += 1
        session.flush()
        if log:
            log(f"  ♻ 已清除既有默认事件 {n} 条，重建")

    recs = [r for r in _records(session) if r]
    if not recs:
        return {"total": 0, "inserted": 0, "skipped": 0}
    # 复用 writer 幂等键 (event_year,title,source_file)
    from app.ingest.writer import import_timeline
    st = import_timeline(session, recs)
    if log:
        log(f"   ⏭ 默认事件 {len(recs)} 条（新增 {st['n']} / 跳过 {st['skipped']}）")
    return {"total": len(recs), "inserted": st["n"], "skipped": st["skipped"]}