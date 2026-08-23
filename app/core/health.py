"""健康校验（DESIGN §10）：H1 时间线对齐 / H2 金额一致 / H3 汇率链 / H4 余额连续 / H5 断链。

run_report(session) -> list[dict]，每条 = {rule, level, location, detail}。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.model import (
    Account, Entity, ExchangeRate, IncomeStream, LedgerEntry, Relationship, ReturnCurve, TimelineEvent,
)


@dataclass
class Finding:
    rule: str          # H1..H5
    level: str         # 'ok' | 'warn' | 'crit'
    location: str      # 文件/行/实体
    detail: str = ""

    def as_dict(self) -> dict:
        return {"rule": self.rule, "level": self.level, "location": self.location, "detail": self.detail}


def check_h1_timeline_alignment(session: Session) -> list[Finding]:
    """H1 时间线对齐：timeline_event 年份 vs income_stream/return_curve 相关年份差异。"""
    finds: list[Finding] = []
    # 时间线事件年份集合 vs income_stream 年份范围：时间线有事件而该年无任何收益流 → warn（可能缺财务）
    income_years = set(session.execute(select(func.distinct(IncomeStream.year))).scalars().all())
    tl_events = session.execute(select(TimelineEvent.event_year, TimelineEvent.title)).all()
    for year, title in tl_events:
        if income_years and year not in income_years:
            finds.append(Finding("H1", "warn", f"时间线 {year}「{title}」", "该年无对应收益流，可能未对齐"))
    return finds


def check_h2_amount_consistency(session: Session) -> list[Finding]:
    """H2 金额一致：income_stream 同 (entity, stream_type, label名, money year) 多来源金额不一致。

    用 label（含具体标的，如具体债券名）作为同类唯一键；同 label 同 year 唯一金额，多来源≠才是冲突。
    """
    finds: list[Finding] = []
    rows = session.execute(
        select(
            IncomeStream.entity_id, IncomeStream.stream_type, IncomeStream.label,
            IncomeStream.group_key, IncomeStream.currency, IncomeStream.year,
            func.count(), func.min(IncomeStream.amount), func.max(IncomeStream.amount),
        ).group_by(IncomeStream.entity_id, IncomeStream.stream_type, IncomeStream.label,
                   IncomeStream.group_key, IncomeStream.currency, IncomeStream.year)
        .having(func.count() > 1, func.min(IncomeStream.amount) != func.max(IncomeStream.amount))
    ).all()
    for eid, st, label, gk, cur, year, cnt, mn, mx in rows:
        ent = session.get(Entity, eid)
        finds.append(Finding("H2", "crit", f"{ent.name if ent else '?'} {st} {cur} {year} [{label}]",
                             f"{cnt}条 金额 {mn} ≠ {mx}"))
    return finds


def check_h3_fx_closure(session: Session) -> list[Finding]:
    """H3 汇率链自洽：A→B→C 闭合回 A→C（简化：同 %年 直接 vs 链式差>0.5% 报）。"""
    finds: list[Finding] = []
    rates = session.execute(select(ExchangeRate)).scalars().all()
    by_pair: dict[tuple[str, str], dict[int, float]] = {}
    for r in rates:
        by_pair.setdefault((r.fx_from, r.fx_to), {})[r.year or 0] = r.rate
    pairs = list(by_pair)
    for (a, b) in pairs:
        for (c, d) in pairs:
            if not (b == c and (a, d) in by_pair):
                continue
            for y in by_pair[(a, b)]:
                v1 = by_pair[(a, b)].get(y, by_pair[(a, b)].get(0))
                v2 = by_pair[(c, d)].get(y, by_pair[(c, d)].get(0))
                direct = by_pair[(a, d)].get(y)
                if v1 and v2 and direct and abs(v1 * v2 - direct) / direct > 0.005:
                    finds.append(Finding("H3", "crit", f"{a}→{b}→{d} @{y}",
                                         f"链式 {v1*v2:.4f} ≠ 直接 {direct}"))
    return finds


def check_h4_balance_chain(session: Session) -> list[Finding]:
    """H4 余额连续：ledger 按 account 排序，后一余额 = 前一 + 入 − 出。"""
    finds: list[Finding] = []
    acct_ids = session.execute(select(func.distinct(LedgerEntry.account_id))).scalars().all()
    for aid in acct_ids:
        entries = session.execute(
            select(LedgerEntry).where(LedgerEntry.account_id == aid)
            .order_by(LedgerEntry.date, LedgerEntry.id)
        ).scalars().all()
        for i in range(1, len(entries)):
            prev, cur = entries[i - 1], entries[i]
            expect = (prev.balance or 0) + (cur.inflow or 0) - (cur.outflow or 0)
            if cur.balance is not None and abs(cur.balance - expect) > 0.005:
                acc = session.get(Account, aid)
                finds.append(Finding("H4", "crit", f"account#{aid}({acc.currency if acc else '?'}) {cur.date}",
                                     f"余额 {cur.balance} ≠ 前{prev.balance}+入{cur.inflow}-出{cur.outflow}={expect:.2f}"))
    return finds


def check_h5_dangling(session: Session) -> list[Finding]:
    """H5 断链：relationship 引用不存在 entity；IncomeStream/Account 引用不存在 entity。"""
    finds: list[Finding] = []
    # relationship
    for rel in session.execute(select(Relationship)).scalars().all():
        if session.get(Entity, rel.from_entity_id) is None or session.get(Entity, rel.to_entity_id) is None:
            finds.append(Finding("H5", "crit", f"relationship#{rel.id}", "引用不存在实体"))
    # IncomeStream → entity
    orphan_ent = set()
    for sid in session.execute(select(func.distinct(IncomeStream.entity_id))).scalars().all():
        if session.get(Entity, sid) is None:
            orphan_ent.add(sid)
    for sid in session.execute(select(func.distinct(Account.entity_id))).scalars().all():
        if session.get(Entity, sid) is None:
            orphan_ent.add(sid)
    for eid in orphan_ent:
        finds.append(Finding("H5", "crit", f"entity#{eid}", "被引用但不存在"))
    return finds


def run_report(session: Session) -> list[dict]:
    """全库健康校验汇总：H1..H5。"""
    all_finds: list[Finding] = []
    all_finds += check_h1_timeline_alignment(session)
    all_finds += check_h2_amount_consistency(session)
    all_finds += check_h3_fx_closure(session)
    all_finds += check_h4_balance_chain(session)
    all_finds += check_h5_dangling(session)
    return [f.as_dict() for f in all_finds]


def summarize(session: Session) -> dict:
    """返回每规则计数（供 overview 汇总）。"""
    report = run_report(session)
    summary: dict[str, dict] = {}
    for r in report:
        s = summary.setdefault(r["rule"], {"total": 0, "warn": 0, "crit": 0})
        s["total"] += 1
        s[r["level"]] = s.get(r["level"], 0) + 1
    return summary