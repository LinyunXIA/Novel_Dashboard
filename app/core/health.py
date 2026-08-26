"""健康校验（DESIGN §10）：H1 时间线对齐 / H2 金额一致 / H3 汇率链 / H4 余额连续 / H5 断链。

run_report(session) -> list[dict]，每条 = {rule, level, location, detail}。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
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


def check_h1_timeline_alignment(session: Session, from_year: int | None = None) -> list[Finding]:
    """H1 时间线对齐：timeline_event 年份 vs income_stream/return_curve 相关年份差异。

    from_year（issue #140 · §9.2d 范围化）：仅检查该年及以后的时间线条目。
    """
    finds: list[Finding] = []
    # 时间线事件年份集合 vs income_stream 年份范围：时间线有事件而该年无任何收益流 → warn（可能缺财务）
    income_years = set(session.execute(select(func.distinct(IncomeStream.year))).scalars().all())
    tl_rows = session.execute(select(TimelineEvent.event_year, TimelineEvent.title)).all()
    for year, title in tl_rows:
        if from_year is not None and year < from_year:
            continue
        if income_years and year not in income_years:
            finds.append(Finding("H1", "warn", f"时间线 {year}「{title}」", "该年无对应收益流，可能未对齐"))
    return finds


def check_h2_amount_consistency(session: Session, from_year: int | None = None) -> list[Finding]:
    """H2 金额一致：income_stream 同 (entity, stream_type, label名, money year) 多来源金额不一致。

    用 label（含具体标的，如具体债券名）作为同类唯一键；同 label 同 year 唯一金额，多来源≠才是冲突。
    from_year（issue #140）：仅统计该年及以后。
    """
    finds: list[Finding] = []
    q = select(
        IncomeStream.entity_id, IncomeStream.stream_type, IncomeStream.label,
        IncomeStream.group_key, IncomeStream.currency, IncomeStream.year,
        func.count(), func.min(IncomeStream.amount), func.max(IncomeStream.amount),
    ).group_by(IncomeStream.entity_id, IncomeStream.stream_type, IncomeStream.label,
               IncomeStream.group_key, IncomeStream.currency, IncomeStream.year)
    if from_year is not None:
        q = q.where(IncomeStream.year >= from_year)
    rows = session.execute(
        q.having(func.count() > 1, func.min(IncomeStream.amount) != func.max(IncomeStream.amount))
    ).all()
    for eid, st, label, _gk, cur, year, cnt, mn, mx in rows:
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


def check_h4_balance_chain(session: Session, from_year: int | None = None) -> list[Finding]:
    """H4 余额连续（复利感知，DESIGN §10「复利/杠杆自洽」口径，issue #113 连锁修正）。

    与 leverage.recompute_one 的年粒度滚动模型同构：
    - 年内非末条分录：纯算术连续（后一 = 前一 + 入 − 出；recompute 不触碰这些行）；
    - 每年最后一条分录：余额 = 年初结转 × (1 + rate_calc) + 该年净流入，
      rate_calc 与 `_rate_for_account_year` 同源（地区×R级×杠杆）；
      无收益率的年份退化为 累计 + 净流入。
    - 首条前无结转 → 视 0（issue #22：不让首条失核逃逸）。
    - from_year（issue #140 · §9.2d 范围化）：结转仍从全史滚出（保证口径一致），
      但只报告该年及以后的失核。
    """
    from collections import defaultdict

    from app.core.leverage import _rate_for_account_year

    finds: list[Finding] = []
    acct_ids = session.execute(select(func.distinct(LedgerEntry.account_id))).scalars().all()
    for aid in acct_ids:
        entries = session.execute(
            select(LedgerEntry).where(LedgerEntry.account_id == aid)
            .order_by(LedgerEntry.date, LedgerEntry.id)
        ).scalars().all()
        if not entries:
            continue
        account = session.get(Account, aid)
        cur_label = f"account#{aid}({account.currency if account else '?'})"

        by_year: dict[int, list] = defaultdict(list)
        for e in entries:
            by_year[e.date.year].append(e)

        carry = 0.0  # 年初结转（上一年的年末校验值）
        # 按日历年全跨度迭代（含无分录的空年）：recompute 对空年同样复利
        for y in range(min(by_year), max(by_year) + 1):
            year_entries = by_year.get(y, [])
            rate_row = _rate_for_account_year(session, account, y) if account else None
            rate = float(rate_row) if rate_row is not None else None
            net_in = sum(float(e.inflow or 0) - float(e.outflow or 0) for e in year_entries)

            if year_entries:
                # 年内非末条：源值算术连续（这些行不被 recompute 改写）；
                # 首条基数为年初结转 carry（与上一年年末校验值衔接）
                run_prev = carry
                for e in year_entries[:-1]:
                    if from_year is not None and e.date.year < from_year:
                        continue
                    if e.balance is None:
                        continue
                    expect = run_prev + float(e.inflow or 0) - float(e.outflow or 0)
                    if abs(float(e.balance) - expect) > 0.01:
                        finds.append(Finding(
                            "H4", "crit", f"{cur_label} {e.date}",
                            f"余额 {e.balance} ≠ 前{run_prev:.2f}+入{e.inflow}-出{e.outflow}={expect:.2f}"))
                    run_prev = float(e.balance)

            if not year_entries:
                # 空年：仅滚动复利结转，无分录可核
                if rate is not None:
                    carry = carry * (1 + rate)
                continue

            last = year_entries[-1]
            if last.balance is None:
                continue
            if rate is not None:
                expect = carry * (1 + rate) + net_in
                formula = f"{carry:.2f}×(1+{rate:.6f})+净流{net_in:.2f}"
            else:
                expect = carry + net_in
                formula = f"{carry:.2f}+净流{net_in:.2f}"
            if abs(float(last.balance) - expect) > 0.01 and (
                    from_year is None or y >= from_year):
                msg = f"年末余额 {last.balance} ≠ {formula}={expect:.2f}"
                if last is entries[0]:
                    msg = f"首条余额 {last.balance} ≠ {formula}={expect:.2f}"
                finds.append(Finding("H4", "crit", f"{cur_label} {last.date}", msg))
            carry = float(last.balance)
    return finds


def check_negative_balance(session: Session, from_year: int | None = None) -> list[Finding]:
    """issue #22：负余额检查。账户余额为负数极可能是计算错误（DESIGN 数值纪律要求
    账户余额非负；个别场景如短贷可豁免但需 source 注释）。

    仅在 balance 列有非 None 值时检查。报 warn（不阻断重算，便于人工复核）。
    from_year（issue #140）：仅检查该年 1 月 1 日起的余额行。
    """
    finds: list[Finding] = []
    q = select(LedgerEntry.account_id, LedgerEntry.date, LedgerEntry.balance).where(
        LedgerEntry.balance < 0)
    if from_year is not None:
        q = q.where(LedgerEntry.date >= date(from_year, 1, 1))
    neg_rows = session.execute(
        q.order_by(LedgerEntry.account_id, LedgerEntry.date)).all()
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
    for hid, company, hdate, eid, _shares, up in rows:
        if up is None or float(up) == 0:
            finds.append(Finding("H-STOCK", "warn", f"holding_event#{hid} {company} {hdate}",
                                 f"shares>0 但 unit_price 缺失（无法估值）"))
        if session.get(Entity, eid) is None:
            finds.append(Finding("H-STOCK", "crit", f"holding_event#{hid} entity#{eid}",
                                 "持仓引用不存在的实体"))
    # ②弱一致：有未结清持仓时，全家族持仓市值应 > 0（除非 unit_price 全缺失——已被①标出）。
    return finds


def check_stock_h2(session: Session) -> list[Finding]:
    """H2 金额一致·股票（F-P2-04 · DESIGN §10/§19.6）：open 持仓的成本批次自洽。

    R1（warn）：同一 (entity, company) 有 >1 个 **buy 源** open 批次且 unit_price 极差 >3× → 疑混成本系/口径错
      （只看 buy；split/acquire-* 的成本随链行不参与，避免 DXC 合并日双源成本系误报）。
    R2（crit）：同一 (entity, company, date) 有 >1 笔 buy/sell 源且 unit_price 不一致 → 同日同公司
      多来源单价打架。**显式只查 buy/sell 源**（排除 split/acquire-* 行），避免 DXC 合并日多源
      （2017-04-01 HPE 源与 CSC 源成本系不同）被误判为冲突。
    """
    finds: list[Finding] = []
    r1 = session.execute(
        select(HoldingEvent.entity_id, HoldingEvent.company,
               func.count(), func.min(HoldingEvent.unit_price), func.max(HoldingEvent.unit_price))
        # 只看 buy 源（手动建仓成本）；split/acquire-* 的 unit_price 由成本随链产生，
        # 同公司多源（如 DXC 合并日 HPE 源 vs CSC 源）成本系差异属正常，不参与离群检测
        .where(HoldingEvent.shares > 0, HoldingEvent.closed_on.is_(None),
               HoldingEvent.event_type == "buy")
        .group_by(HoldingEvent.entity_id, HoldingEvent.company)
        .having(func.count() > 1)
    ).all()
    for eid, comp, cnt, mn, mx in r1:
        if mn and mx and float(mx) / float(mn) > 3.0:
            ent = session.get(Entity, eid)
            finds.append(Finding("H2", "warn", f"{comp} [{ent.name if ent else '?'}]",
                                 f"{cnt}个 open 批次 unit_price {mn}..{mx}（>3×，疑混成本系）"))
    r2 = session.execute(
        select(HoldingEvent.entity_id, HoldingEvent.company, HoldingEvent.date,
               func.count(), func.min(HoldingEvent.unit_price), func.max(HoldingEvent.unit_price))
        # 四轮审计 #169：仅比 buy 间源单价——sell.unit_price 是 FIFO 平均成本，
        # 同日 buy+sell 单价几乎必然不等，纳入会系统性 crit 噪声
        .where(HoldingEvent.event_type == "buy",
               HoldingEvent.unit_price.isnot(None))
        .group_by(HoldingEvent.entity_id, HoldingEvent.company, HoldingEvent.date)
        .having(func.count() > 1,
                func.min(HoldingEvent.unit_price) != func.max(HoldingEvent.unit_price))
    ).all()
    for eid, comp, d, cnt, mn, mx in r2:
        finds.append(Finding("H2", "crit", f"{comp} {d}",
                             f"{cnt}笔 buy 源 unit_price {mn} ≠ {mx}"))
    return finds


def run_report(session: Session, from_year: int | None = None) -> list[dict]:
    """全库健康校验汇总：H1..H5 + H-STOCK。

    from_year（issue #140 · §9.2d）：范围限定——H1/H2/H4/负余额只报告该年及以后；
    H3/H5/H-STOCK 为全局完整性规则（廉价且天然全局），不受影响。
    """
    all_finds: list[Finding] = []
    all_finds += check_h1_timeline_alignment(session, from_year)
    all_finds += check_h2_amount_consistency(session, from_year)
    all_finds += check_fx_coverage(session)
    all_finds += check_h3_fx_closure(session)
    all_finds += check_negative_balance(session, from_year)
    all_finds += check_h4_balance_chain(session, from_year)
    all_finds += check_h5_dangling(session)
    all_finds += check_holding_value(session)
    all_finds += check_stock_h2(session)
    return [f.as_dict() for f in all_finds]


def summarize(session: Session, from_year: int | None = None) -> dict:
    """返回每规则计数（供 overview 汇总；from_year 语义同 run_report）。"""
    report = run_report(session, from_year)
    # issue #23 / API 健康视图：预填 H1..H5 即使 0 也有键，前端可稳定读 summary['H1']
    summary: dict[str, dict] = {rule: {"total": 0, "warn": 0, "crit": 0}
                                for rule in ("H1", "H2", "H3", "H4", "H5", "H-STOCK")}
    for r in report:
        s = summary.setdefault(r["rule"], {"total": 0, "warn": 0, "crit": 0})
        s["total"] += 1
        s[r["level"]] = s.get(r["level"], 0) + 1
    return summary