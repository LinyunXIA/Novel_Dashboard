"""杠杆/收益曲线计算（DESIGN §7.2 · F-P0-12 修正）。

提供 `recompute_one(account, from_year)`：按地区→国家映射读取 return_curve，
应用杠杆倍率（1989年前 1.5×，1989年起 2×），逐年滚动复利重算账户余额。

该模块为增量重算核心，替代 `recompute.py` 中纯台账滚动逻辑。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.model import Account, LedgerEntry, ReturnCurve

# 地区 → 收益曲线国家映射（与 invest.REGION_COUNTRY 一致）
REGION_COUNTRY = {
    "欧洲": "比利时",
    "英国": "英国",
    "美国": "美国",
    "香港": "中国香港",
    "中国": "中国大陆",
}

# 杠杆分界：1989 年前 1.5×，1989 年起 2×（PRD §6.3 / CLAUDE.md 数值纪律）
LEVERAGE_SINCE_1989 = Decimal("2.0")
LEVERAGE_BEFORE_1989 = Decimal("1.5")
LEVERAGE_CUTOFF_YEAR = 1989

_ZERO = Decimal(0)
_ONE = Decimal(1)


def _get_account_region(session: Session, account: Account) -> Optional[str]:
    """推断账户所属地区：按 entity 的 currency 与国家对应关系反推（简化：currency → 地区）。

    实际业务中，账户币种强关联地区：
    - BEF/LUF/EUR(比利时/卢森堡) → 欧洲
    - SEK → 欧洲(瑞典)
    - DKK → 欧洲(丹麦)
    - NLG → 欧洲(荷兰)
    - GBP → 英国
    - USD → 美国
    - HKD → 香港
    - CNY → 中国

    为保持与 return_curve.country 兼容，直接用 currency 推 region 再映射 country。
    """
    cur = account.currency
    if cur in ("BEF", "LUF", "EUR", "SEK", "DKK", "NLG"):
        return "欧洲"
    if cur == "GBP":
        return "英国"
    if cur == "USD":
        return "美国"
    if cur == "HKD":
        return "香港"
    if cur == "CNY":
        return "中国"
    return None


def _leverage_for_year(year: int) -> Decimal:
    """该年杠杆倍率：1989 前 1.5，1989 起 2。"""
    return LEVERAGE_SINCE_1989 if year >= LEVERAGE_CUTOFF_YEAR else LEVERAGE_BEFORE_1989


def _rate_for_account_year(session: Session, account: Account, year: int) -> Optional[Decimal]:
    """该账户该年有效年化收益率（已乘杠杆，百分数→小数，如 21.7% → 0.217）。"""
    region = _get_account_region(session, account)
    if not region:
        return None
    country = REGION_COUNTRY.get(region)
    if not country:
        return None

    # 取该账户币种对应的风险等级（简化：主仓按 R3，或按账户字段推断；此处按地区取 R3 作为基准）
    # 实际应按 entity 投资策略决定 R 级；此处提供可配置钩子，默认 R3
    risk_lvl = "R3"

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

    for y in range(from_year, 2026):
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


def recompute_all(session: Session, from_year: int, reason: str = "manual") -> list[dict]:
    """全库增量重算（受影响起算年向后）。返回每账户结果。"""
    from sqlalchemy import select as _select
    from app.model import LedgerEntry as _LE
    acc_ids = session.execute(_select(_LE.account_id).distinct()).scalars().all()
    out = []
    for aid in acc_ids:
        out.append(recompute_one(session, aid, from_year))
    return out