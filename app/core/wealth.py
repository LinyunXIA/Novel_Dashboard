"""财富曲线视图（DESIGN P0-2 / §7）：按账户×币种 + 全家族合计 + USD 展示折算。

账务本币记录（snapshot 已是本币），展示层按 exchange_rate 折 USD。

数值纪律（F-P0-? 修复 #2）：
- 汇率缺失时绝不静默 fallback 到 1.0；返回 None 让调用方扣出该币种
  并在响应里挂 missing_rates 显式告警（dev 库即此状态）。
- 反向分支 currency→USD 同样按基准常量（year IS NULL）回退，保持与正向分支对称。

issue #12：family_total_usd 改读 family:total 快照（避免逐账户实时折算）；
账户/币种维度仍走 account:* / entity:* 快照。
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.model import ExchangeRate, Snapshot


def _usd_rate(session: Session, currency: str, year: int) -> float | None:
    """currency → USD 折算率（1 单位 currency = X USD）。

    返回 None 表示汇率缺失；调用方必须 1) 跳过该币种贡献，2) 记入 missing_rates。
    绝不静默返回 1.0 防止 BEF/DKK/NLG/SEK 裸加当美元（issue #2 根因）。
    """
    if currency == "USD":
        return 1.0
    # 正向：USD→<currency> 行，rate 是 1 USD 兑多少 currency；取倒数
    row = session.execute(
        select(ExchangeRate.rate).where(
            ExchangeRate.fx_from == "USD", ExchangeRate.fx_to == currency,
            or_(ExchangeRate.year == year, ExchangeRate.year.is_(None)),
        )
        # 具体年份优先于基准常量（NULL 排后）
        .order_by(ExchangeRate.year.is_(None), ExchangeRate.year.desc())
        .limit(1)
    ).first()
    if row is not None and row[0] is not None:
        return 1.0 / float(row[0])
    # 反向：<currency>→USD 行，按基准常量（year IS NULL）回退保持对称
    row2 = session.execute(
        select(ExchangeRate.rate).where(
            ExchangeRate.fx_from == currency, ExchangeRate.fx_to == "USD",
            or_(ExchangeRate.year == year, ExchangeRate.year.is_(None)),
        )
        .order_by(ExchangeRate.year.is_(None), ExchangeRate.year.desc())
        .limit(1)
    ).first()
    return float(row2[0]) if row2 is not None and row2[0] is not None else None


def _missing_rates_from_snaps(session: Session, snaps: list[Snapshot], year: int) -> list[str]:
    """从一组快照筛出该年汇率缺失的币种（用于 wealth_series 告警）。"""
    out: set[str] = set()
    for sn in snaps:
        cur = sn.currency or "USD"
        if cur == "USD":
            continue
        if _usd_rate(session, cur, year) is None and float(sn.value or 0.0) != 0:
            out.add(cur)
    return sorted(out)


def family_total_usd(session: Session, year: int) -> dict:
    """该年全家族合计（USD）—— 直读 family:total 快照（issue #12）。

    返回 {"family_total_usd": float, "missing_rates": [(currency, year)...]}
    汇率缺失时该币种不计入合计；missing_rates 给前端显式告警（通过 account:* 快照反推）。
    """
    fam = session.execute(
        select(Snapshot.value).where(
            Snapshot.as_of_year == year, Snapshot.scope == "family:total",
            Snapshot.as_of_date.is_(None),
        ).limit(1)
    ).scalar_one_or_none()
    # missing_rates 仍走 account:* 快照反推（family:total 已固化为已折算值）
    account_snaps = session.execute(
        select(Snapshot).where(
            Snapshot.as_of_year == year,
            Snapshot.scope.like("account:%"),
            Snapshot.as_of_date.is_(None),
        )
    ).scalars().all()
    missing = [(c, year) for c in _missing_rates_from_snaps(session, account_snaps, year)]
    return {"family_total_usd": round(float(fam or 0.0), 2),
            "missing_rates": missing}


def wealth_series(session: Session, year_from: int = 1947, year_to: int = 2025) -> dict:
    """逐年 {year: {family_total_usd, accounts, currencies, missing_rates}}。

    issue #12：family_total_usd 直接读 family:total 快照（O(1)），
    accounts/currencies 仍按 account:* 快照聚合。missing_rates 从 account:* 快照反推。
    """
    out: dict[int, dict] = {}
    # 一次性把所有年份的快照取回来（避免逐 year 多次查询）
    all_snaps = session.execute(
        select(Snapshot).where(
            Snapshot.as_of_year >= year_from, Snapshot.as_of_year <= year_to,
            Snapshot.as_of_date.is_(None),
        )
    ).scalars().all()
    # family_total_usd 索引：year → value
    fam_by_year: dict[int, float] = {}
    # account_snaps_by_year：year → list[Snapshot]（仅 account:* 行）
    acct_by_year: dict[int, list[Snapshot]] = defaultdict(list)
    for sn in all_snaps:
        if sn.scope == "family:total":
            fam_by_year[sn.as_of_year] = float(sn.value or 0.0)
        elif sn.scope.startswith("account:"):
            acct_by_year[sn.as_of_year].append(sn)
    for y in range(year_from, year_to + 1):
        account_snaps = acct_by_year.get(y, [])
        accounts: dict[str, float] = {}
        currencies: dict[str, float] = defaultdict(float)
        for sn in account_snaps:
            val = float(sn.value or 0.0)
            cur = sn.currency or "USD"
            accounts[sn.scope] = val
            currencies[cur] += val
        out[y] = {
            "family_total_usd": round(fam_by_year.get(y, 0.0), 2),
            "accounts": accounts,
            "currencies": dict(currencies),
            "missing_rates": _missing_rates_from_snaps(session, account_snaps, y),
        }
    return out