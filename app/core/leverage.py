"""杠杆/收益曲线计算（DESIGN §7.2 · F-P0-12 修正 · issue #113 口径定案）。

提供 `recompute_one(account, from_year)`：按地区→国家映射读取 return_curve，
应用杠杆倍率（1989年前 1.5×，1989年起 2×），逐年滚动复利重算账户余额。

**复利为 opt-in**：仅 `entity.fields["compound"]=true` 的账户参与 §7.2 滚动；
普通源台账账户（含自带收益明细行者）文件即权威、纯算术连续（PRD §6.10）。
H4 健康校验经 `_rate_for_account_year` 同源取率，两模块口径恒一致。

该模块为增量重算核心，替代 `recompute.py` 中纯台账滚动逻辑。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import CALENDAR_MAX_YEAR
from app.core.regions import (
    CURRENCY_REGION, DEFAULT_RISK_LVL, REGION_COUNTRY,
    entity_region_override, entity_risk_override,
)
from app.model import Account, LedgerEntry, ReturnCurve

# 地区 → 收益曲线国家映射：单一权威定义见 app/core/regions.py（issue #113：
# 旧字面量 欧洲→比利时 等在 return_curve 中无对应国家行，收益查询恒 None）

# 杠杆分界：1989 年前 1.5×，1989 年起 2×（PRD §6.3 / CLAUDE.md 数值纪律）
LEVERAGE_SINCE_1989 = Decimal("2.0")
LEVERAGE_BEFORE_1989 = Decimal("1.5")
LEVERAGE_CUTOFF_YEAR = 1989

_ZERO = Decimal(0)
_ONE = Decimal(1)


def _get_account_region(session: Session, account: Account) -> Optional[str]:
    """推断账户所属地区（issue #113）：

    优先级：entity.fields["return_region"] 显式覆盖 > 币种推断（CURRENCY_REGION）。
    币种归属（CLAUDE.md 币种纪律）：BEF/LUF/EUR/SEK/DKK/NLG→欧洲、GBP→英国、
    USD→美国、HKD→香港、CNY→中国。源 canon 仅 5 份地区测算表，欧洲内部
    不再细分国家（PRD §6.7「不细分国家」同源口径）。
    """
    fields = getattr(getattr(account, "entity", None), "fields", None)
    override = entity_region_override(fields)
    if override:
        return override
    return CURRENCY_REGION.get(account.currency)


def _leverage_for_year(year: int) -> Decimal:
    """该年杠杆倍率：1989 前 1.5，1989 起 2。"""
    return LEVERAGE_SINCE_1989 if year >= LEVERAGE_CUTOFF_YEAR else LEVERAGE_BEFORE_1989


def _rate_for_account_year(session: Session, account: Account, year: int) -> Optional[Decimal]:
    """该账户该年有效年化收益率（已乘杠杆，百分数→小数，如 21.7% → 0.217）。

    **opt-in 门禁（issue #113 口径定案 A）**：仅 entity.fields["compound"]=true
    的账户启用 §7.2 曲线×杠杆复利——自带收益明细行的源台账（如「杠杆投资收益R5」
    入账行）默认不复利，避免双重计息；文件即权威（PRD §6.10）。
    H4 健康校验经本函数同源取率，普通账户自动退化为纯算术连续。

    R 级：entity.fields["risk_lvl"] 覆盖 > 默认 R3；地区：fields["return_region"]
    覆盖 > 币种推断。
    """
    fields = getattr(getattr(account, "entity", None), "fields", None)
    if not (fields and fields.get("compound") is True):
        return None

    region = _get_account_region(session, account)
    if not region:
        return None
    country = REGION_COUNTRY.get(region)
    if not country:
        return None

    risk_lvl = entity_risk_override(fields) or DEFAULT_RISK_LVL

    row = session.execute(
        select(ReturnCurve.rate).where(
            ReturnCurve.country == country,
            ReturnCurve.risk_lvl == risk_lvl,
            ReturnCurve.year == year,
        ).limit(1)
    ).first()
    if row is None or row[0] is None:
        return None

    base_rate = Decimal(str(row[0])) / Decimal(100)  # 百分数转小数
    leverage = _leverage_for_year(year)
    return (base_rate * leverage).quantize(Decimal("0.000001"))


def recompute_one(session: Session, account_id: int, from_year: int) -> dict:
    """从 from_year 起重算单账户余额链（含杠杆复利）。

    口径：balance_y = balance_{y-1} * (1 + rate_calc) + net_inflow_y
    - rate_calc = return_curve.rate% * leverage / 100
    - net_inflow_y = 该年 Σ(inflow) - Σ(outflow) （ledger 实测值）
    - 基线：from_year 前最后一条分录的 balance（若无则 0）

    返回：{"account_id", "from_year", "entries", "updated"}
    """
    account = session.get(Account, account_id)
    if not account:
        return {"account_id": account_id, "from_year": from_year, "entries": 0, "updated": 0}

    entries = session.execute(
        select(LedgerEntry).where(LedgerEntry.account_id == account_id)
        .order_by(LedgerEntry.date, LedgerEntry.id)
    ).scalars().all()

    # 基线：from_year 前最后一条分录的余额
    baseline: Decimal = _ZERO
    for e in entries:
        if e.date.year < from_year and e.balance is not None:
            baseline = Decimal(e.balance)
        elif e.date.year >= from_year:
            break

    balance: Decimal = baseline
    years_updated = 0

    # 按年聚合流水，逐年滚动
    from collections import defaultdict
    year_in: dict[int, Decimal] = defaultdict(lambda: _ZERO)
    year_out: dict[int, Decimal] = defaultdict(lambda: _ZERO)
    for e in entries:
        if e.date.year >= from_year:
            year_in[e.date.year] += Decimal(e.inflow) if e.inflow is not None else _ZERO
            year_out[e.date.year] += Decimal(e.outflow) if e.outflow is not None else _ZERO

    for y in range(from_year, CALENDAR_MAX_YEAR + 1):
        net_inflow = year_in.get(y, _ZERO) - year_out.get(y, _ZERO)
        rate = _rate_for_account_year(session, account, y)
        if rate is not None:
            balance = balance * (_ONE + rate) + net_inflow
        else:
            balance = balance + net_inflow

        # 写回该年最后一条分录的 balance（若无分录则不写；快照层会读取计算值）
        year_entries = [e for e in entries if e.date.year == y]
        if year_entries:
            last_entry = year_entries[-1]
            if last_entry.balance is None or Decimal(last_entry.balance) != balance:
                last_entry.balance = balance
                years_updated += 1

    return {"account_id": account_id, "from_year": from_year, "entries": len(entries), "updated": years_updated}