"""健康校验（DESIGN §10）：H1 时间线对齐 / H2 金额一致 / H3 汇率链 / H4 余额连续 / H5 断链。

run_report(session) -> list[dict]，每条 = {rule, level, location, detail}。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.model import (
    Account, Entity, ExchangeRate, HoldingEvent, IncomeStream, LedgerEntry, Relationship, ReturnCurve, TimelineEvent,
)

from app.core.stock_wealth import portfolio_breakdown


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
    """H3 汇率链自洽：A→B→C 闭合回 A→C（简化：同 %年 直接 vs 链式差>0.5% 报）。

    issue #22：direct 侧也回退到常量键 0，与链式两侧对称；任一侧缺值则跳过该年。
    """
    finds: list[Finding] = []
    rates = session.execute(select(ExchangeRate)).scalars().all()
    by_pair: dict[tuple[str, str], dict[int, float]] = {}
    for r in rates:
        by_pair.setdefault((r.fx_from, r.fx_to), {})[r.year or 0] = r.rate
    pairs = list(by_pair)

    def _val(pair: tuple[str, str], y: int) -> float | None:
        """统一 NULL 回退：y → 常量键 0；都无则 None（跳过判定）。"""
        return pair_data.get(y, pair_data.get(0)) if (pair_data := by_pair.get(pair)) else None

    for (a, b) in pairs:
        for (c, d) in pairs:
            if not (b == c and (a, d) in by_pair):
                continue
            for y in by_pair[(a, b)]:
                v1 = _val((a, b), y)
                v2 = _val((c, d), y)
                direct = _val((a, d), y)
                if v1 is None or v2 is None or direct is None or direct == 0:
                    continue
                if abs(v1 * v2 - direct) / direct > 0.005:
                    finds.append(Finding("H3", "crit", f"{a}→{b}→{d} @{y}",
                                         f"链式 {v1*v2:.4f} ≠ 直接 {direct}"))
    return finds


def check_h4_balance_chain(session: Session) -> list[Finding]:
    """H4 余额连续：ledger 按 account 排序，后一余额 = 前一 + 入 − 出。

    issue #22：从 i=0 起，首条 prev 视为 0（无前余额），单独校验
    balance == inflow - outflow（不让首条失核逃逸）。
    """
    finds: list[Finding] = []
    acct_ids = session.execute(select(func.distinct(LedgerEntry.account_id))).scalars().all()
    for aid in acct_ids:
        entries = session.execute(
            select(LedgerEntry).where(LedgerEntry.account_id == aid)
            .order_by(LedgerEntry.date, LedgerEntry.id)
        ).scalars().all()
        for i, cur in enumerate(entries):
            prev_bal = 0 if i == 0 else (entries[i - 1].balance or 0)
            if cur.balance is None:
                continue
            expect = prev_bal + (cur.inflow or 0) - (cur.outflow or 0)
            if abs(cur.balance - expect) > 0.005:
                acc = session.get(Account, aid)
                if i == 0:
                    msg = f"首条余额 {cur.balance} ≠ 入{cur.inflow}-出{cur.outflow}={expect:.2f}"
                else:
                    msg = (f"余额 {cur.balance} ≠ 前{prev_bal}+入{cur.inflow}-出{cur.outflow}={expect:.2f}")
                finds.append(Finding("H4", "crit",
                                     f"account#{aid}({acc.currency if acc else '?'}) {cur.date}", msg))
    return finds


def check_negative_balance(session: Session) -> list[Finding]:
    """issue #22：负余额检查。账户余额为负数极可能是计算错误（DESIGN 数值纪律要求
    账户余额非负；个别场景如短贷可豁免但需 source 注释）。

    仅在 balance 列有非 None 值时检查。报 warn（不阻断重算，便于人工复核）。
    """
    finds: list[Finding] = []
    neg_rows = session.execute(
        select(LedgerEntry.account_id, LedgerEntry.date, LedgerEntry.balance)
        .where(LedgerEntry.balance < 0)
        .order_by(LedgerEntry.account_id, LedgerEntry.date)
    ).all()
    for aid, dt, bal in neg_rows:
        acc = session.get(Account, aid)
        finds.append(Finding("H4", "warn",
                             f"account#{aid}({acc.currency if acc else '?'}) {dt}",
                             f"负余额 {bal}"))
    return finds


def check_fx_coverage(session: Session) -> list[Finding]:
    """issue #22：汇率表覆盖提示。空表 → warn（USD 折算空转不可见）；
    有数据但所有行 year=NULL → warn（按基准常量折算，无法逐年）。

    不报具体覆盖率——account/ledger 跨年缺失率留给应用层计算；这里只暴露最
    显著的两种盲区（完全空 / 全部常量）。
    """
    finds: list[Finding] = []
    total = session.execute(select(func.count()).select_from(ExchangeRate)).scalar() or 0
    if total == 0:
        finds.append(Finding("H3", "warn", "exchange_rate", "表为空，USD 折算空转不可见"))
        return finds
    null_year = session.execute(
        select(func.count()).select_from(ExchangeRate).where(ExchangeRate.year.is_(None))
    ).scalar() or 0
    if null_year == total:
        finds.append(Finding("H3", "warn", "exchange_rate", "全部 year=NULL，仅基准常量折算"))
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


def check_holding_value(session: Session) -> list[Finding]:
    """H-STOCK 持仓估值一致性（F-P2-02 §19.6；不改 H1–H5 语义）。

    ① shares>0（未结清）的 holding_event 行必须有 unit_price，否则无法估值 → warn；
    ② shares>0 的 holding 须落在已存在 entity 上（被引用实体缺失 → crit，与 H5 同源防漏）。
    """
    finds: list[Finding] = []
    rows = session.execute(
        select(HoldingEvent.id, HoldingEvent.company, HoldingEvent.date,
               HoldingEvent.entity_id, HoldingEvent.shares, HoldingEvent.unit_price)
        .where(HoldingEvent.shares > 0, HoldingEvent.closed_on.is_(None))
    ).all()
    for hid, company, hdate, eid, shares, up in rows:
        if up is None or float(up) == 0:
            finds.append(Finding("H-STOCK", "warn", f"holding_event#{hid} {company} {hdate}",
                                 f"shares>0 但 unit_price 缺失（无法估值）"))
        if session.get(Entity, eid) is None:
            finds.append(Finding("H-STOCK", "crit", f"holding_event#{hid} entity#{eid}",
                                 "持仓引用不存在的实体"))
    # ②弱一致：有未结清持仓时，全家族持仓市值应 > 0（除非 unit_price 全缺失——已被①标出）。
    return finds


def run_report(session: Session) -> list[dict]:
    """全库健康校验汇总：H1..H5 + H-STOCK。"""
    all_finds: list[Finding] = []
    all_finds += check_h1_timeline_alignment(session)
    all_finds += check_h2_amount_consistency(session)
    all_finds += check_fx_coverage(session)
    all_finds += check_h3_fx_closure(session)
    all_finds += check_negative_balance(session)
    all_finds += check_h4_balance_chain(session)
    all_finds += check_h5_dangling(session)
    all_finds += check_holding_value(session)
    return [f.as_dict() for f in all_finds]


def summarize(session: Session) -> dict:
    """返回每规则计数（供 overview 汇总）。"""
    report = run_report(session)
    # issue #23 / API 健康视图：预填 H1..H5 即使 0 也有键，前端可稳定读 summary['H1']
    summary: dict[str, dict] = {rule: {"total": 0, "warn": 0, "crit": 0}
                                for rule in ("H1", "H2", "H3", "H4", "H5", "H-STOCK")}
    for r in report:
        s = summary.setdefault(r["rule"], {"total": 0, "warn": 0, "crit": 0})
        s["total"] += 1
        s[r["level"]] = s.get(r["level"], 0) + 1
    return summary