"""逐年 as-of 快照预计算（DESIGN §8）。

口径：as_of_year 状态 = 固定值(初始) + 累计到该年的年标记变更（income − expense）。
对每个账户逐年归算余额，写入 snapshot(scope='account:{id}:{cur}')；并按 entity 聚合
（scope='entity:{id}:{cur}'）以及家族合计（scope='family:total'，USD 口径）。

issue #12 修复：
- 新增 from_year 参数：仅重建 from_year 起的快照（delete 限定 as_of_year >= from_year）
- 补 entity:* scope（按 entity_id × 币种聚合）
- 补 family:total scope（USD 口径家族合计）

issue #28 修复：
- 内部计算全程 Decimal（避免 float 二进制误差累积）
- 0 值且无流水的年份跳过写入（account/entity/family 三种 scope），避免快照表膨胀

issue #85 修复：
- 净值口径对齐 §19.4：净值 = 银行 + 专款池合计。投资在途（划出后~赎回前）本金不使
  family/entity 净值凹陷——entity/family 聚合时经 `pool_in_transit` 加回在途本金；
  account scope 仍为纯银行余额。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.currency import usd_rate
from app.model import Account, IncomeStream, LedgerEntry, Snapshot

# 写库精度：保留 2 位小数（与原 round(v, 2) 一致）
_QUANTIZE_2 = Decimal("0.01")
_ZERO = Decimal(0)


def account_balance_at(session: Session, account_id: int, cutoff) -> Decimal:
    """账户截至 cutoff（date 级）的余额：Σ(inflow) − Σ(outflow) for date <= cutoff。

    与 _account_balance_series 同源口径：在 cutoff=12-30（某年年末）时两者相等，
    保证日历按日累加与预计算年度快照一致（issue #17 方案 A′）。

    issue #28：返回 Decimal 而非 float，避免上层 cast 丢精度。
    """
    tin, tout = session.execute(
        select(func.coalesce(func.sum(LedgerEntry.inflow), 0),
               func.coalesce(func.sum(LedgerEntry.outflow), 0))
        .where(LedgerEntry.account_id == account_id, LedgerEntry.date <= cutoff)
    ).one()
    return Decimal(tin or 0) - Decimal(tout or 0)


def _close_year_of(acc: Account) -> int | None:
    """账户关池年：closed 且有关池日 → closed_on.year，否则 None（DESIGN §6.6）。"""
    if acc.status == "closed" and acc.closed_on is not None:
        return acc.closed_on.year
    return None


def pool_in_transit(session: Session, as_of: date) -> dict[tuple[int, str], Decimal]:
    """各 (entity_id, currency) 截至 as_of 的在途专款池本金（DESIGN §19.4 / issue #85）。

    口径：净值 = 银行 + 专款池合计。投资本金在 `start_date` 划出银行入池（kind='investment'
    ledger 支出）、至该投资年 12-30 划回（kind='pool' 流入）；**在途期间该笔资金归入专款池
    而非令净值凹陷**。本实现走 ledger 累计：在途本金 = Σ(kind='investment' 支出) −
    Σ(kind='pool' 流入)，截止 as_of——按日期天然与银行账目一致，且不依赖 redeemed_at
    （那只作防重标记，非结算日）。

    返回 {(entity_id, currency): 在途本金}；无在途资金返回空 dict。供
    - calendar.snapshot_as_of（日级 as-of）与
    - rebuild_snapshots #5/#4（年）在聚合 family:total / entity:* 时加回，
    保持「投资期间总净值不变」。
    """
    rows = session.execute(
        select(
            Account.entity_id,
            Account.currency,
            func.coalesce(func.sum(func.coalesce(LedgerEntry.outflow, 0)), 0),
            func.coalesce(func.sum(func.coalesce(LedgerEntry.inflow, 0)), 0),
        )
        .join(Account, Account.id == LedgerEntry.account_id)
        .where(LedgerEntry.date <= as_of, LedgerEntry.kind.in_(["investment", "pool"]))
        .group_by(Account.entity_id, Account.currency)
    ).all()
    out: dict[tuple[int, str], Decimal] = {}
    for eid, cur, o, i in rows:
        net = Decimal(o or 0) - Decimal(i or 0)       # 划出(投资) − 划回(pool本金)
        if net != 0:
            out[(int(eid), cur)] = net
    return out


def _account_balance_series(session: Session, account_id: int,
                            years: range, close_year: int | None = None) -> dict[int, Decimal]:
    """该账户逐年 as-of 余额：初始现金 + 累计 income − 累计 expense。

    关池（close_year）后不双计：closed 账户自关池年起余额清零——钱已结转进承接币种
    （EUR）账户，避免与承接分录叠加（DESIGN §6.6）。

    issue #28：全程 Decimal；返回 dict[year, Decimal]。
    """
    # ledger 收支（现金进 ledger）
    rows = session.execute(
        select(func.extract("year", LedgerEntry.date),
               func.coalesce(func.sum(LedgerEntry.inflow), 0),
               func.coalesce(func.sum(LedgerEntry.outflow), 0))
        .where(LedgerEntry.account_id == account_id)
        .group_by(func.extract("year", LedgerEntry.date))
    ).all()
    year_in: dict[int, Decimal] = {}
    year_out: dict[int, Decimal] = {}
    for y, tin, tout in rows:
        year_in[int(y)] = Decimal(tin or 0)
        year_out[int(y)] = Decimal(tout or 0)
    # 逐年滚动
    series: dict[int, Decimal] = {}
    bal: Decimal = _ZERO
    for y in years:
        bal = bal + year_in.get(y, _ZERO) - year_out.get(y, _ZERO)
        series[y] = _ZERO if close_year is not None and y >= close_year else bal
    return series


def _ownership_accounts(session: Session) -> list[Account]:
    return session.execute(select(Account)).scalars().all()


def _usd_rate(session: Session, currency: str, year: int) -> Decimal | None:
    """currency → USD 折算率（issue #71：委托 core/currency.py 权威实现）。"""
    return usd_rate(session, currency, year)


def _holding_entity_ids(holding_by_year: dict[int, dict[int, Decimal]]) -> set[int]:
    """有股票持仓的主体 id 集（跨年并集；#169 对齐 calendar 的 entity 键构建）。"""
    out: set[int] = set()
    for ymap in holding_by_year.values():
        out.update(ymap.keys())
    return out


def rebuild_snapshots(session: Session, years: range = None,
                      from_year: int | None = None) -> dict:
    """重建逐年账户/实体/家族三层快照。

    years 缺省 = config.calendar_years()（issue #141：上限不再写死 2025/2026）；
    from_year=None → 全量重建（1947 起）；
    from_year=N    → 仅重建 [N, end] 年；旧 [1947, N-1] 快照保留（§9.2c 增量）。

    issue #28：value=0 且无流水的年份跳过写入（account/entity/family 三种 scope），
    避免 79 年×每账户 每账户必写 79 行的膨胀；汇率缺失币种不计入 family（保留原语义）。

    返回 {"snapshots": 行数, "accounts": 账户数, "entities": 实体数, "family_years": 家族快照年数}
    """
    if years is None:
        from app.config import calendar_years
        years = calendar_years()
    stats = {"snapshots": 0, "accounts": 0, "entities": 0, "family_years": 0}
    years_list = list(years)
    if not years_list:
        return stats
    start = from_year if from_year is not None else years_list[0]

    # 0) 专款池在途（issue #85 §19.4）：逐年 12-30 在途本金，供 entity/family 聚合时加回，
    #    保证年度净值口径 = 银行 + 专款池合计（投资在途不凹陷）。
    pool_by_year: dict[int, dict[tuple[int, str], Decimal]] = {}
    for y in years_list:
        if y < start:
            continue
        pool_by_year[y] = pool_in_transit(session, date(y, 12, 30))

    # 0b) 股票持仓市值（F-P2-02 §19.6）：逐年 12-30 口径 → {year: {entity_id: Decimal(USD元)}}。
    #     只进 entity/family 两域，绝不进 account 域（持仓不在银行账户）。
    from app.core.stock_wealth import portfolio_breakdown
    holding_by_year: dict[int, dict[int, Decimal]] = {}
    for y in years_list:
        if y < start:
            continue
        holding_by_year[y] = {eid: Decimal(str(v))
                              for eid, v in portfolio_breakdown(session, date(y, 12, 30)).items()}

    # 1) 清旧：仅清 from_year 起（含）的三种 scope 行
    session.execute(
        delete(Snapshot).where(
            Snapshot.as_of_year >= start,
            Snapshot.as_of_date.is_(None),
        )
    )

    # 2) 先把所有账户余额 series 算好（避免 entity 聚合时再算）
    series_by_acc: dict[int, dict[int, Decimal]] = {}
    for acc in _ownership_accounts(session):
        series_by_acc[acc.id] = _account_balance_series(
            session, acc.id, years, close_year=_close_year_of(acc))

    # 3) 写 account:* 行（issue #28：value=0 跳过；Decimal.quantize(0.01) 写回）
    for acc in _ownership_accounts(session):
        series = series_by_acc[acc.id]
        written = False
        for y in years_list:
            if y < start:
                continue
            v = series.get(y, _ZERO)
            if v == 0:
                continue
            session.add(Snapshot(
                as_of_year=y, as_of_date=None,
                scope=f"account:{acc.id}:{acc.currency}",
                value=v.quantize(_QUANTIZE_2), currency=acc.currency,
            ))
            stats["snapshots"] += 1
            written = True
        if written:
            stats["accounts"] += 1

    # 4) 写 entity:* 行（entity × currency 聚合；0 值跳过）
    accs = session.execute(select(Account)).scalars().all()
    entity_agg: dict[tuple[int, str], dict[int, Decimal]] = {}
    for acc in accs:
        key = (acc.entity_id, acc.currency)
        ea = entity_agg.setdefault(key, {})
        series = series_by_acc.get(acc.id, {})
        for y, v in series.items():
            if y < start:
                continue
            ea[y] = ea.get(y, _ZERO) + v
    # 四轮审计 #169：与 calendar.snapshot_as_of 对齐——无 USD 银行账户但持有股票的
    # 主体也要有 entity:{eid}:USD 行（此前仅 calendar 建、年度快照缺该行，两接口数值不同）
    for eid in {eid for eid, cur in
                [(e, "USD") for e in _holding_entity_ids(holding_by_year)]}:
        entity_agg.setdefault((eid, "USD"), {})
    for (eid, cur), ymap in entity_agg.items():
        written = False
        for y in years_list:
            if y < start:
                continue
            # §19.4：净值 = 银行 + 专款池在途；entity 口径把该年 12-30 在途本金加回（issue #85）
            v = ymap.get(y, _ZERO) + pool_by_year[y].get((eid, cur), _ZERO)
            # F-P2-02 §19.6：仅当该币种是 USD 时追加股票持仓市值（第一阶段全 USD；FX 后续）
            if cur == "USD":
                v = v + holding_by_year[y].get(eid, _ZERO)
            if v == 0:
                continue
            session.add(Snapshot(
                as_of_year=y, as_of_date=None,
                scope=f"entity:{eid}:{cur}",
                value=v.quantize(_QUANTIZE_2), currency=cur,
            ))
            stats["snapshots"] += 1
            written = True
        if written:
            stats["entities"] += 1

    # 5) 写 family:total 行（USD 口径；汇率缺失币种不计入；全账户 0 时跳过该年）
    for y in years_list:
        if y < start:
            continue
        family_usd: Decimal = _ZERO
        any_contribution = False
        for acc in accs:
            series = series_by_acc.get(acc.id, {})
            v = series.get(y, _ZERO)
            if v == 0:
                continue
            rate = _usd_rate(session, acc.currency, y)
            if rate is None:
                continue                                 # 汇率缺失 → 不计入
            family_usd = family_usd + v * rate
            any_contribution = True
        # §19.4：净值 = 银行 + 专款池在途；family:total 补回该年 12-30 在途本金（issue #85）。
        # 在途本金按主体×币种记（account 银行余额已含划出凹陷，这里加回对冲）。
        for (eid, cur), amt in pool_by_year[y].items():
            rate = _usd_rate(session, cur, y)
            if rate is None:
                continue                                 # 汇率缺失 → 不计入
            family_usd = family_usd + amt * rate
            any_contribution = True
        # F-P2-02 §19.6：股票持仓市值（USD，无需折率）计入 family:total
        for usd_val in holding_by_year[y].values():
            if usd_val:
                family_usd = family_usd + usd_val
                any_contribution = True
        # issue #28：family_usd=0 跳过（避免家族层面多年 0 值行膨胀）
        if not any_contribution or family_usd == 0:
            continue
        session.add(Snapshot(
            as_of_year=y, as_of_date=None,
            scope="family:total",
            value=family_usd.quantize(_QUANTIZE_2), currency="USD",
        ))
        stats["snapshots"] += 1
        stats["family_years"] += 1
    return stats