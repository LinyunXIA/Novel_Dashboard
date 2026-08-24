"""投资功能核心（DESIGN §19.1–19.4 · F-P1-01/02）。

UI 派生的第三类改数据通道：选 年份 × 地区（对应 return_curve.country）× R 级 ×
【主体 × 币种 × 金额/$全部】提交 → 划出银行入专款池 → 年末(12-30)赎回本金+收益回银行。
一年一次（UNIQUE(year, region)）；已投锁灰，解锁=整笔抹除重输，已赎回不可解锁。

issue #80：UI 派生落 finance_entry（划出 kind='investment'；赎回 kind='pool'+'investment_income'），
            source='ui' —— 财务收支屏据此可见 UI 派生数据。
issue #81：解锁(locked=False) 整笔抹除本投资的全部派生写入（ledger/finance/timeline，按 note/label
            标签 `inv#{id}` 定位）+ 覆盖分支同语义），杜绝重输双扣；已赎回投资拒绝解锁/覆盖(409)。
issue #82：赎回按笔防重——redeemed_at 非空即 409（不再按年扫全库 pool 流入，避免同年多地区互堵）；
            GET 据 redeemed_at 暴露 redeemed。

数值纪律：
- 计息公式（§19.2）：days=(当年12-30 − start_date)；gross=R/100/365×days；
  interest=principal×gross；start_date=12-30 → days=0、收益=0；R 可为负（亏损 count）。
- R 来自本地 `return_curve(country=REGION_COUNTRY[region], risk_lvl, year)`（确定性 SQL，
  LLM 不参与，§18.1 铁律）。
- 全程 Decimal（对齐 issue #28：避免 float 误差进账本）。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.snapshot import account_balance_at
from app.model import (
    Account, Entity, FinanceEntry, Investment, InvestmentAlloc, LedgerEntry,
    ReturnCurve, TimelineEvent,
)
from app.model.types import SourceKind

# 区域 → 收益曲线国家（return_curve.country）映射（DESIGN §19.1）
REGION_COUNTRY = {
    "欧洲": "比利时",
    "英国": "英国",
    "美国": "美国",
    "香港": "中国香港",
    "中国": "中国大陆",
}
# 区域起始年下限（DESIGN §19.3）：UI 选项下限 + serve 校验（422）
REGION_START_YEAR = {
    "欧洲": 1947,
    "英国": 1983,
    "美国": 1989,
    "香港": 1999,
    "中国": 2002,
}

_ZERO = Decimal(0)


class InvestmentError(Exception):
    """投资业务错误基类；status 供 API 映射（422 参数 / 409 冲突）。"""

    def __init__(self, detail: str, status: int = 422):
        super().__init__(detail)
        self.detail = detail
        self.status = status


class ValidationError(InvestmentError):
    def __init__(self, detail: str):
        super().__init__(detail, 422)


class ConflictError(InvestmentError):
    def __init__(self, detail: str):
        super().__init__(detail, 409)


def _inv_tag(inv) -> str:
    """投资派生写入的定位标签（inv#{id}），供解锁/覆盖时整体抹除。"""
    return f"inv#{inv.id}"


def region_start_years() -> dict:
    """返回 区域 → (起始年, 收益国家)，供 API/前端下拉下限。"""
    return {r: {"start_year": REGION_START_YEAR[r], "country": REGION_COUNTRY[r]}
            for r in REGION_START_YEAR}


def _primary_account(session: Session, entity_id: int, currency: str) -> Optional[Account]:
    """该主体该币种的记账账户（主账户，取最小 id 的 active；同币种池唯一时即它）。

    UNIQUE(entity_id, currency, bank) 允许同主体多 bank 同币种账户；投资划出/赎回
    统一落在最小 id 的 active 账户，保证余额链可重算（实测每主体每币种仅一个账户）。

    关池（status=closed）账户不接受——§6.6 BEF/LUF/NLG 2002-01-01 关池后只读终态，
    若主体该币种仅 closed 池，应走 EUR 承接池而非 silent 回退（issue #83 收紧）。
    返回 None 以便调用方显式拒绝（Error 422 含币种/主体）。
    """
    return session.execute(
        select(Account).where(
            Account.entity_id == entity_id,
            Account.currency == currency,
            Account.status == "active",
        ).order_by(Account.id).limit(1)
    ).scalar_one_or_none()


def _pool_balance(session: Session, entity_id: int, currency: str, cutoff: date) -> Decimal:
    """该主体该币种资金池在 cutoff 的可用余额：所有该币种 active 账户 as-of 之和。"""
    accs = session.execute(
        select(Account).where(Account.entity_id == entity_id,
                              Account.currency == currency,
                              Account.status == "active")
    ).scalars().all()
    total: Decimal = _ZERO
    for a in accs:
        total += account_balance_at(session, a.id, cutoff)
    return total


def _simulate_outflow_nonneg(session: Session, account_id: int,
                             on_date: date, amount: Decimal) -> bool:
    """覆盖连锁/破负预检：在 on_date 追加一笔 amount 支出后，账户自该日起是否全程非负。

    §19.3「覆盖连锁拒绝」：改写动向后按向后全链 as-of 滚动，任一年拐负 → 整体拒绝。
    返回 False 表示会拐负（须拒绝）。与 transfer._simulate 同一口径（§19.4 复用向后传播）。
    """
    rows = session.execute(
        select(LedgerEntry).where(LedgerEntry.account_id == account_id)
        .order_by(LedgerEntry.date, LedgerEntry.id)
    ).scalars().all()
    bal: Decimal = _ZERO
    applied = False
    for e in rows:
        if not applied and e.date >= on_date:
            bal -= amount
            applied = True
            if bal < _ZERO:
                return False
        bal += (Decimal(e.inflow) if e.inflow is not None else _ZERO)
        bal -= (Decimal(e.outflow) if e.outflow is not None else _ZERO)
        if bal < _ZERO:
            return False
    if not applied:
        bal -= amount
        return bal >= _ZERO
    return True


def _rate(session: Session, region: str, risk_lvl: str, year: int) -> Optional[Decimal]:
    """该地区该年该 R 级年化收益（return_curve.country 对应当前 region）。"""
    country = REGION_COUNTRY[region]
    row = session.execute(
        select(ReturnCurve.rate).where(
            ReturnCurve.country == country,
            ReturnCurve.risk_lvl == risk_lvl,
            ReturnCurve.year == year,
        ).limit(1)
    ).first()
    return Decimal(row[0]) if row and row[0] is not None else None


def _delete_investment_writes(session: Session, inv: Investment) -> None:
    """整笔抹除该投资落下的全部派生写入（issue #81）：ledger 划出/赎回、finance_entry、
    overlay 时间线条目——按 note/label 标签 `inv#{id}` 精确定位，避免误删他人数据。

    调用方（解锁/覆盖）须随后 recompute + rebuild_snapshots，恢复账户 to 投资前。
    """
    tag = _inv_tag(inv)
    session.execute(delete(LedgerEntry).where(LedgerEntry.note.like(f"%{tag}%")))
    session.execute(delete(FinanceEntry).where(FinanceEntry.label.like(f"%{tag}%")))
    session.execute(delete(TimelineEvent).where(
        TimelineEvent.note.like(f"%{tag}%"),
        TimelineEvent.overlay.is_(True),
    ))


def unlock_investment(session: Session, investment: Investment) -> Investment:
    """§19.1「解锁 = 整笔抹除重输」：抹除该投资全部派生写入 + 置 locked=False（issue #81）。

    已赎回投资拒绝解锁（409）。抹除后账户 as-of 恢复 to 投资前；重输走 create_investment
    的 unlocked 覆盖分支（同样整笔抹除）。调用方随后须 recompute_all + rebuild_snapshots。
    """
    if investment.redeemed_at is not None:
        raise ConflictError(f"该投资 {investment.region} {investment.year} 已赎回，不可解锁")
    if not investment.locked:
        return investment  # 已解锁，幂等
    _delete_investment_writes(session, investment)
    investment.locked = False
    return investment


def _entity_kind(session: Session, entity_id: int) -> str:
    """finance_entry.entity_kind：按 entity.entity_type，限定 person/company。"""
    e = session.get(Entity, entity_id)
    return e.entity_type if e and e.entity_type in ("person", "company") else "person"


def compute_interest(session: Session, investment: Investment) -> list[dict]:
    """§19.2 计息：逐 alloc 返回 {entity_id, currency, days, rate, gross, principal, interest}。

    days=(该年12-30 − start_date)；start_date=12-30 → days=0 → interest=0（R 可负）。
    该年该地区该 R 无值且 days>0 → ValidationError(422)。
    """
    country = REGION_COUNTRY[investment.region]
    rate = _rate(session, investment.region, investment.risk_lvl, investment.year)
    settlement = date(investment.year, 12, 30)
    days = (settlement - investment.start_date).days
    days = max(days, 0)
    if rate is None and days > 0:
        raise ValidationError(
            f"该年 {investment.year} 该地区 {investment.region}({country}) R{investment.risk_lvl} 无收益值，无法计息")
    # gross = R/100/365×days（R 是百分数，如 21.7 → 21.7%）
    gross = (rate / Decimal(100) / Decimal(365) * Decimal(days)) if rate is not None else _ZERO
    out = []
    for alloc in session.execute(
            select(InvestmentAlloc).where(InvestmentAlloc.investment_id == investment.id)
    ).scalars().all():
        principal = Decimal(alloc.amount or 0)
        interest = (principal * gross).quantize(Decimal("0.01"))
        out.append({
            "entity_id": alloc.entity_id,
            "currency": alloc.currency,
            "days": days,
            "rate": float(rate) if rate is not None else None,
            "gross": float(gross),
            "principal": float(principal),
            "interest": float(interest),
        })
    return out


def redeem_investment(session: Session, investment: Investment) -> dict:
    """§19.2 年末赎回：本金+收益从专款池划回银行（12-30）、专款池清空。

    每 alloc 两笔 inflow：本金 kind='pool' + 收益 kind='investment_income'；
    并落 finance_entry（source='ui'，issue #80）。余额 None 交由 recompute 回填。

    防重（issue #82）：按本笔 redeemed_at 判重，非空 → 409；不再按年扫全库，同年多地区互不阻塞。
    审计修复：年末结算 gate——未到当年 12-30 拒绝赎回（409），防误触提前锁定全年收益
    （历史年份结算日已过，不受影响）。
    """
    if investment.redeemed_at is not None:
        raise ConflictError(f"该投资 {investment.region} {investment.year} 已赎回，勿重复")
    settlement = date(investment.year, 12, 30)
    if date.today() < settlement:
        raise ConflictError(
            f"该投资 {investment.region} {investment.year} 未到年末结算日 {settlement}，"
            f"不可提前赎回")
    interests = compute_interest(session, investment)
    tag = _inv_tag(investment)
    made = 0
    for it in interests:
        acc = _primary_account(session, it["entity_id"], it["currency"])
        if acc is None:
            raise ValidationError(f"主体 #{it['entity_id']} 无 {it['currency']} 账户，无法赎回")
        ek = _entity_kind(session, it["entity_id"])
        # 本金划回（kind=pool）
        session.add(LedgerEntry(
            account_id=acc.id, date=settlement, reason=f"赎回本金 {investment.region} R{investment.risk_lvl}",
            inflow=Decimal(it["principal"]), kind="pool",
            note=f"UI 投资赎回本金 {tag}",
        ))
        session.add(FinanceEntry(
            entity_id=it["entity_id"], entity_kind=ek, year=investment.year, kind="pool",
            amount=Decimal(it["principal"]), currency=it["currency"],
            label=f"投资赎回本金 {tag}", source=SourceKind.UI.value,
        ))
        # 收益划回（kind=investment_income）
        session.add(LedgerEntry(
            account_id=acc.id, date=settlement, reason=f"投资损益 {investment.region} R{investment.risk_lvl}",
            inflow=Decimal(it["interest"]), kind="investment_income",
            note=f"UI 投资赎回收益 {tag}",
        ))
        session.add(FinanceEntry(
            entity_id=it["entity_id"], entity_kind=ek, year=investment.year,
            kind="investment_income", amount=Decimal(it["interest"]), currency=it["currency"],
            label=f"投资赎回收益 {tag}", source=SourceKind.UI.value,
        ))
        made += 1
    # 编年史 overlay 同步（§19.5 记年）
    session.add(TimelineEvent(
        event_year=investment.year, event_date=settlement,
        title=f"{investment.region} R{investment.risk_lvl} 投资赎回",
        note=f"本金+收益划回银行，专款池清空（{tag}）",
        decade=f"{investment.year // 10 * 10}s", overlay=True,
    ))
    investment.redeemed_at = datetime.now()
    return {"investment_id": investment.id, "allocs": made}


def create_investment(session: Session, *, year: int, region: str, risk_lvl: str,
                      start_date: date, allocs: list[dict]) -> Investment:
    """创建投资（§19.1/§19.3 校验链 + §19.2 划出 + §19.x finance_entry 镜像，issue #80）。

    allocs = [{entity_id, currency, amount(float|None), is_all(bool)}]；
    is_all=True → amount 忽略，取该主体该币种 as-of 全投。

    校验链（顺序）：
      1. region 起始年下限 → 422；
      2. (year, region) 年度幂等：已存在 locked → 409（须解锁重输）；unlocked → **整笔抹除旧写入**
         后覆盖重建（issue #81）；已赎回 → 409（不可覆盖）；
      3. R 级合法 + 无收益值且跨天 → 422；
      4. 每 alloc：amount>0 且 ≤ 该主体该币种 as-of 余额（is_all 取全量）→ 422；
      5. 覆盖连锁拒绝：划出后账户向后全程非负，否则 → 422。
    成功后：写 Investment+Alloc → 划出走账(kind='investment') + finance_entry(kind='investment',
    source='ui') → 编年史 overlay。调用方负责 flush/commit + recompute_all + rebuild_snapshots。
    """
    # 1) 区域起始年下限
    if region not in REGION_START_YEAR:
        raise ValidationError(f"未知地区 {region!r}，可选 {list(REGION_START_YEAR)}")
    if year < REGION_START_YEAR[region]:
        raise ValidationError(
            f"地区 {region} 起始年不早于 {REGION_START_YEAR[region]}（当前 {year}）")
    if risk_lvl not in ("R1", "R2", "R3", "R4", "R5"):
        raise ValidationError(f"risk_lvl 必须为 R1–R5，得到 {risk_lvl!r}")
    # issue #93：投资发生日必须落在该投资年份 `year` 内且不晚于该年结算日 12-30，
    # 否则计息年份（days 按 date(year,12,30)−start_date）与 Investment.year 字段错位、
    # ledger 划出也与年份不一致。
    if start_date.year != year or start_date > date(year, 12, 30):
        raise ValidationError(
            f"投资发生日 {start_date} 必须落在 {year} 年内且不晚于 {year}-12-30 结算日")

    # 2) 年度幂等（issue #81：覆盖须整笔抹除旧写入，避免重输双扣）
    existing = session.execute(
        select(Investment).where(Investment.year == year, Investment.region == region).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.locked:
            raise ConflictError(f"该年 {year} 该地区 {region} 已投（locked），须解锁重输")
        if existing.redeemed_at is not None:
            raise ConflictError(f"该年 {year} 该地区 {region} 投资已赎回，不可覆盖")
        # issue #81：不依赖 DB FK CASCADE（SQLite 测试未开外键；Postgres 也显式稳健）——
        # 显式删 allocs + 派生写入后再删 investment 行
        _delete_investment_writes(session, existing)
        session.execute(delete(InvestmentAlloc).where(InvestmentAlloc.investment_id == existing.id))
        session.delete(existing)
        session.flush()

    # 3) 收益值预检（跨天的投资必须能取值；days=0 允许无值）
    rate = _rate(session, region, risk_lvl, year)
    days = (date(year, 12, 30) - start_date).days
    if rate is None and days > 0:
        raise ValidationError(
            f"该年 {year} 地区 {region}({REGION_COUNTRY[region]}) 无 R{risk_lvl} 收益值")

    # 4) 每 alloc：余额校验 + 落账
    # 审计修复：批内同主体同币种多笔 alloc 按「累计占用」校验——as-of 上限、破负模拟
    # 都以 committed 累计额为准，杜绝两笔各自合法、合计超额/中途拐负漏拦。
    inv = Investment(year=year, region=region, risk_lvl=risk_lvl,
                     start_date=start_date, locked=True)
    session.add(inv)
    session.flush()
    committed: dict[tuple[int, str], Decimal] = {}
    for a in allocs:
        eid = int(a["entity_id"])
        cur = a["currency"]
        key = (eid, cur)
        is_all = bool(a.get("is_all", False))
        already = committed.get(key, _ZERO)
        pool = _pool_balance(session, eid, cur, start_date)
        if is_all:
            # 「全部」= 扣除批内已占后的剩余全投；重复「全部」→ 剩余 ≤ 0 被下方拦截
            amount = pool - already
        else:
            amount = Decimal(str(a["amount"]))
        if amount <= _ZERO:
            raise ValidationError(
                f"主体 #{eid} 币种 {cur} 投资额必须 > 0（as-of {pool}，批内已占 {already}）")
        if amount + already > pool:
            raise ValidationError(
                f"主体 #{eid} 币种 {cur} 批内累计投入 {amount + already} "
                f"超过 as-of 余额 {pool}（{start_date}）")
        # 5) 覆盖连锁拒绝：向后全链不破负（按批内累计口径模拟）
        acc = _primary_account(session, eid, cur)
        if acc is not None and not _simulate_outflow_nonneg(session, acc.id, start_date,
                                                            amount + already):
            raise ValidationError(
                f"主体 #{eid} 币种 {cur} 该划出致账户 {start_date} 起走向负值，整体拒绝")
        committed[key] = amount + already
        session.add(InvestmentAlloc(investment_id=inv.id, entity_id=eid,
                                    currency=cur, amount=amount, is_all=is_all))

    # 划出走账（kind='investment'，balance 由 recompute 回填）+ finance_entry 镜像（source='ui'）
    tag = _inv_tag(inv)
    for alloc in session.execute(
            select(InvestmentAlloc).where(InvestmentAlloc.investment_id == inv.id)
    ).scalars().all():
        acc = _primary_account(session, alloc.entity_id, alloc.currency)
        if acc is None:
            raise ValidationError(f"主体 #{alloc.entity_id} 无 {alloc.currency} 账户")
        session.add(LedgerEntry(
            account_id=acc.id, date=start_date,
            reason=f"划入专款池 {region} R{risk_lvl} {alloc.currency}",
            outflow=Decimal(alloc.amount), kind="investment",
            note=f"UI 投资划出 {tag}",
        ))
        session.add(FinanceEntry(
            entity_id=alloc.entity_id, entity_kind=_entity_kind(session, alloc.entity_id),
            year=year, kind="investment", amount=Decimal(alloc.amount),
            currency=alloc.currency, label=f"投资 {region} R{risk_lvl} {tag}",
            source=SourceKind.UI.value,
        ))
    session.add(TimelineEvent(
        event_year=year, event_date=start_date,
        title=f"{region} R{risk_lvl} 投资 {[_a['currency'] for _a in allocs]}",
        note=f"投入专款池，年末赎回（{tag}）",
        decade=f"{year // 10 * 10}s", overlay=True,
    ))
    session.flush()
    return inv