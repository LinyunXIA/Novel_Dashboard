"""活期利息（DESIGN §19.2 · 审计修复补齐）。

§19.2：「活期：未划拨资金留银行账户，默认 2% 年化按日折；不设独立余额」。

年度结息（结算日 12-30）：对每个 active 账户，按台账逐段余额加权
Σ(余额 × 持有天数) × 2% ÷ 365 计一笔利息 inflow 入账：
- ledger_entry(kind='income', note 含 `demand#{year}` 定位标签)
- finance_entry(kind='income', source='ui') 镜像——财务收支屏可见（issue #80 同口径）
- 编年史 overlay 直写 timeline_event(overlay=True)（issue #86 决策备注同款）

幂等：重跑同年度先按 `demand#{year}` 标签整笔抹除旧写入再重记
（与投资解锁 inv#{id} 抹除同模式，§19.1）。未到结算日的年份拒绝（422），
防对未发生的余额凭空计息。

数值纪律：全程 Decimal；窗口 [当年-01-01, 当年-12-30]，窗口前流水滚入基线、
窗口后流水不影响本年计息；加权余额 ≤ 0 或 quantize 后利息为 0 的账户跳过
（负余额不罚息、碎额不生噪行）；关池账户（§6.6 只读终态）不参与。
LLM 不参与任何数值（确定性台账回放，§18.1 铁律）。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.invest import ValidationError, _entity_kind, delete_derived_by_tag
from app.model import Account, FinanceEntry, LedgerEntry, TimelineEvent
from app.model.types import SourceKind

DEMAND_RATE = Decimal("0.02")          # §19.2 默认 2% 年化
_Q2 = Decimal("0.01")
_ZERO = Decimal(0)


def _tag(year: int) -> str:
    """活期结息派生写入的定位标签，供幂等重跑整笔抹除。"""
    return f"demand#{year}"


def demand_settlement_date(year: int) -> date:
    """结息结算日：当年 12-30（全系统统一结算口径 §6.2）。"""
    return date(year, 12, 30)


def _dec(x) -> Decimal:
    return Decimal(x) if x is not None else _ZERO


def weighted_balance_days(session: Session, account_id: int, year: int) -> Decimal:
    """该账户在 [01-01, 12-30] 的 Σ(逐段余额 × 持有天数)（确定性台账回放）。

    - 基线：窗口前所有流水的累计净额；
    - 窗口内每条流水切一段：上一余额 × 距上一天数；
    - 收尾：最后余额 × 至结算日天数。
    """
    start = date(year, 1, 1)
    settle = demand_settlement_date(year)
    rows = session.execute(
        select(LedgerEntry).where(LedgerEntry.account_id == account_id)
        .order_by(LedgerEntry.date, LedgerEntry.id)
    ).scalars().all()
    bal: Decimal = _ZERO
    weighted: Decimal = _ZERO
    prev = start
    for e in rows:
        if e.date < start:
            bal += _dec(e.inflow) - _dec(e.outflow)
            continue
        if e.date > settle:
            break
        weighted += bal * Decimal((e.date - prev).days)
        bal += _dec(e.inflow) - _dec(e.outflow)
        prev = e.date
    weighted += bal * Decimal((settle - prev).days)
    return weighted


def quote_demand_interest(session: Session, year: int) -> list[dict]:
    """试算该年各 active 账户活期利息（只读，不写库）。

    返回 [{account_id, entity_id, currency, weighted_days, interest}]，
    仅含 quantize 后利息 > 0 的账户（负余额不罚息、0 利息不生噪行）。
    """
    out: list[dict] = []
    accs = session.execute(select(Account).where(Account.status == "active")).scalars().all()
    for acc in accs:
        wd = weighted_balance_days(session, acc.id, year)
        if wd <= _ZERO:
            continue
        interest = (wd * DEMAND_RATE / Decimal(365)).quantize(_Q2)
        if interest <= _ZERO:
            continue
        out.append({"account_id": acc.id, "entity_id": acc.entity_id,
                    "currency": acc.currency, "weighted_days": float(wd),
                    "interest": interest})
    return out


def accrue_demand_interest(session: Session, year: int) -> dict:
    """该年活期结息（§19.2）：先抹旧（`demand#{year}` 标签）再重记，幂等。

    校验：未到结算日（今天 < 当年 12-30）→ ValidationError(422)。
    写入：每账户一笔 ledger inflow(kind='income') + finance_entry 镜像(source='ui')
    + 一条编年史 overlay 汇总条目。调用方随后须 recompute_all + rebuild_snapshots。
    返回 {"year", "accounts", "total_by_currency": {币种: 利息}}。
    """
    settle = demand_settlement_date(year)
    if date.today() < settle:
        raise ValidationError(f"活期结息须在结算日 {settle} 之后操作（当前 {date.today()}）")

    tag = _tag(year)
    # issue #137：词边界抹除复用 invest.delete_derived_by_tag（demand#{year} 不得命中他年）
    delete_derived_by_tag(session, tag=tag)

    quotes = quote_demand_interest(session, year)
    total_by_cur: dict[str, Decimal] = {}
    for q in quotes:
        acc = session.get(Account, q["account_id"])
        session.add(LedgerEntry(
            account_id=acc.id, date=settle,
            reason=f"活期结息 {year}（2% 年化按日折）",
            inflow=q["interest"], kind="income",
            note=f"UI 活期结息 {tag}",
        ))
        session.add(FinanceEntry(
            entity_id=acc.entity_id, entity_kind=_entity_kind(session, acc.entity_id),
            year=year, kind="income", amount=q["interest"], currency=acc.currency,
            label=f"活期利息 {tag}", source=SourceKind.UI.value,
        ))
        total_by_cur[acc.currency] = total_by_cur.get(acc.currency, _ZERO) + q["interest"]

    detail = "、".join(f"{c} {v.quantize(_Q2)}" for c, v in sorted(total_by_cur.items())) or "无应计"
    session.add(TimelineEvent(
        event_year=year, event_date=settle,
        title=f"活期结息 {year}",
        note=f"active 账户 2% 年化按日折：{detail}（{tag}）",
        decade=f"{year // 10 * 10}s", overlay=True,
    ))
    session.flush()
    return {"year": year, "accounts": len(quotes),
            "total_by_currency": {c: float(v.quantize(_Q2)) for c, v in total_by_cur.items()}}
