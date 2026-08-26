"""事件·股票：并购/分拆三形态成本随链引擎（F-P2-03 · DESIGN §19.6）。

三种形态（S19.6）：
1. 纯换股/分拆（split）按新股数占比摊成本、现金不动。
2. 换股+现金（cash_share）股票腿成本全随链、现金腿入余额且不冲减成本/不记损益。
3. 纯现金（cash）持仓归 0、现金入余额、不记损益。

纯函数输入归一化批次 list，可离线单测；apply_merger 做 DB 写入（holding_event 新批次
+ 结清旧行 + 现金 ledger）。事件 spec 见各函数 docstring。
"""
from __future__ import annotations

from datetime import date as _date
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.model import HoldingEvent, LedgerEntry

# ---------------------------------------------------------------------------
# 纯函数（批次 batch = {shares, unit_price[, batch_id]}）
# ---------------------------------------------------------------------------
def split_position(batches: list[dict], legs: list[dict]) -> list[dict]:
    """形态1 纯换股/分拆：旧批次成本按「新股份数占比」分摊到各新证券。

    legs = [{company, per_old_share}]（每持 1 旧股得 per_old_share 新股，如 UTC 1→1CARR/0.5OTIS）。
    保持 per-batch FIFO 粒度：每旧批对每腿生成一行。
    """
    per_sum = sum(leg["per_old_share"] for leg in legs) or 1.0
    result: list[dict] = []
    for bt in batches:
        cost_total = bt["shares"] * bt["unit_price"]
        for leg in legs:
            shares = bt["shares"] * leg["per_old_share"]
            cost = cost_total * leg["per_old_share"] / per_sum
            result.append({"company": leg["company"], "shares": shares,
                           "unit_price": cost / shares if shares else 0.0,
                           "from_batch": bt.get("batch_id")})
    return result


def cash_share_position(batches: list[dict], legs: list[dict], cash_per_share: float) -> tuple[list[dict], float]:
    """形态2 换股+现金：股票腿成本全额随链（多腿按股数占比）；现金腿=Σ旧股×cash_per_share。

    返回 (新批次, cash)。现金不冲减成本、不记损益。"""
    total_old = sum(b["shares"] for b in batches)
    cost_total = sum(b["shares"] * b["unit_price"] for b in batches)
    per_sum = sum(leg["per_old_share"] for leg in legs) or 1.0
    new: list[dict] = []
    for leg in legs:
        shares = total_old * leg["per_old_share"]
        cost = cost_total * leg["per_old_share"] / per_sum
        new.append({"company": leg["company"], "shares": shares,
                    "unit_price": cost / shares if shares else 0.0})
    cash = total_old * cash_per_share
    return new, cash


def cash_merger(batches: list[dict], cash_per_share: float) -> float:
    """形态3 纯现金：持仓归 0，仅返回现金 = Σ旧股 × cash_per_share。不记损益。"""
    return sum(b["shares"] for b in batches) * cash_per_share


# ---------------------------------------------------------------------------
# 事件 spec + DB 写入
# ---------------------------------------------------------------------------
# spec = {"entity_id":int, "date":"YYYY-MM-DD", "old_company":str,
#         "form":"split"|"cash_share"|"cash",
#         "legs":[{"company":..,"per_old_share":..}],
#         "cash_per_share":float, "cash_account_id":int|None}

def _open_batches(db: Session, entity_id: int, company: str) -> list[dict]:
    """读未结清（shares>0 且 closed_on IS NULL）的公司批次，依日期归并为批次。"""
    rows = db.execute(select(HoldingEvent).where(
        HoldingEvent.entity_id == entity_id,
        HoldingEvent.company == company,
        HoldingEvent.shares > 0,
        # sell/pseudo 是历史/占比标记，非 open 持仓（否则分红/抬升/估值会误计）
        HoldingEvent.event_type != "sell",
        HoldingEvent.event_type != "pseudo",
        # 已结清（分拆/并购/全量卖出标记 closed_on）不再视为 open
        HoldingEvent.closed_on.is_(None),
    ).order_by(HoldingEvent.date, HoldingEvent.id)).scalars().all()
    return [{"shares": float(r.shares), "unit_price": float(r.unit_price or 0.0),
             "batch_id": r.batch_id} for r in rows]


def _next_batch_ids(db: Session, n: int) -> Iterator[int]:
    m = db.execute(select(HoldingEvent.batch_id).order_by(HoldingEvent.batch_id.desc()).limit(1)) \
        .scalar_one_or_none()
    start = (m or 0) + 1
    return iter(range(start, start + n))


def _already_applied(db: Session, spec: dict, source: str | None = None) -> bool:
    """并购事件是否已应用。幂等键 = legs[0].company + date（+ source，若提供了）。

    若无 source（F-P2-03 直接调 apply_merger 的场景），沿用 company+date 判定。
    若提供了 source（F-P2-04 链驱动，source=event_id），则额外要求 source_file 匹配——
    避免「同一天多源都产出同一公司」（如 2017-04-01 HPE→DXC 与 CSC→DXC）互相误挡。
    """
    if not spec.get("legs"):
        return False
    first_company = spec["legs"][0]["company"]
    q = select(HoldingEvent.id).where(
        HoldingEvent.entity_id == spec["entity_id"],
        HoldingEvent.company == first_company,
        HoldingEvent.date == spec["date"],
    )
    if source:
        q = q.where(HoldingEvent.source_file == source)
    return db.execute(q.limit(1)).scalar_one_or_none() is not None


def apply_merger(db: Session, spec: dict, source: str | None = None) -> dict:
    """对持仓主体应用一次并购/分拆事件：写新批次 + 结清旧行 + 现金 ledger。返回 {new_batches, cash, closed}。"""
    form = spec.get("form")
    entity_id = spec["entity_id"]
    old_company = spec["old_company"]
    event_date = _date.fromisoformat(spec["date"])   # spec 日期字符串 → date 对象

    if _already_applied(db, spec, source):
        return {"new_batches": [], "cash": 0.0, "closed": 0, "skipped": True}

    batches = _open_batches(db, entity_id, old_company)
    # 捕获事件前已存在的旧 open 行 id——结清只关这些（保住同名腿：如 HPQ→{HPQ,1},{HPE,1}
    # 中 HPQ 同名腿不应被关，否则父头寸丢失）。
    old_ids = [rid for rid, in db.execute(select(HoldingEvent.id).where(
        HoldingEvent.entity_id == entity_id,
        HoldingEvent.company == old_company,
        HoldingEvent.shares > 0,
        HoldingEvent.closed_on.is_(None))).all()]
    new: list[dict] = []
    cash = 0.0
    closed = 0

    if batches:
        closed = len(batches)
        nid = _next_batch_ids(db, len(batches) * max(len(spec.get("legs") or []), 1) + (bool(cash)))
        if form == "split":
            new = split_position(batches, spec["legs"])
            evtype = "split"
        elif form == "cash_share":
            new, cash = cash_share_position(batches, spec["legs"], spec.get("cash_per_share") or 0.0)
            evtype = "acquire-share"
        elif form == "cash":
            cash = cash_merger(batches, spec.get("cash_per_share") or 0.0)
            evtype = "acquire-cash"
        else:
            raise ValueError(f"未知 form: {form}")

        # 写新批次
        for nb in new:
            db.add(HoldingEvent(
                entity_id=entity_id, company=nb["company"],
                ticker=next((l.get("ticker") for l in spec.get("legs") or [] if l["company"] == nb["company"]), None),
                date=event_date, event_type=evtype, batch_id=next(nid),
                shares=nb["shares"], unit_price=nb["unit_price"],
                amount=nb["shares"] * nb["unit_price"] / 10000.0,  # 万USD 口径
                source_file=source))
        # 结清旧行：标记 closed_on（保留 shares/unit_price 历史供重构前年份 as-of 估值），
        # 而非销毁 shares=0（否则分拆/并购前年份市值漏记）。估值按 closed_on 时间窗求值。
        # 只关事件前已存在的旧行（old_ids），不关刚生成的同名腿。
        for oid in old_ids:
            r = db.get(HoldingEvent, oid)
            if r is not None:
                r.closed_on = event_date

        # 现金腿 → ledger
        if cash and spec.get("cash_account_id"):
            db.add(LedgerEntry(
                account_id=spec["cash_account_id"], date=event_date,
                reason=f"并购现金对价·{old_company}{'→'+','.join(l['company'] for l in spec.get('legs') or []) if spec.get('legs') else '退市'}",
                inflow=cash, balance=None, kind="investment_income", note=f"股权事件·{form}"))

    return {"new_batches": new, "cash": cash, "closed": closed, "skipped": False}


# ---------------------------------------------------------------------------
# F-P2-02：买入 / FIFO 卖出 / 分红 / 被动抬升（§19.6）
# ---------------------------------------------------------------------------
# 通用幂等：每个动作接受 event_id（幂等 nonce），写入 HoldingEvent.source_file；
# 闸门按 (entity_id, source_file) 判：已存在 → skipped。ledger note 统一打
# `股票事件·{event_id}`（参照 invest._delete_investment_writes 的 tag 约定，可撤销）：
#   撤销 = 按 note LIKE '%股票事件·{event_id}%' 删 ledger + source_file==event_id 删 holding_event。
# ledger kind 复用约束内类型（income/expense/investment/investment_income/pool），不加 DDL。
# 买入/卖出/分红/抬升与块 A (stock_wealth.market_value_at) 都用 shares×unit_price 口径，
# 保证「总资产 = 现金 + 专款池 + 持仓市值」不重不漏。


def _event_nonce_applied(db: Session, entity_id: int, event_id: str) -> bool:
    """是否已应用该事件（按 (entity_id, source_file=event_id) 判重）。"""
    return db.execute(select(HoldingEvent.id).where(
        HoldingEvent.entity_id == entity_id,
        HoldingEvent.source_file == event_id).limit(1)).scalar_one_or_none() is not None


def _require_open_account(db: Session, account_id: int) -> None:
    """五轮审计 #176：关池（closed）账户只读终态（§6.6），拒新流水——
    手动 buy/sell/dividend 与事件关联共用同一防线（ValueError → API 层 422）。
    六轮收敛 #179 附注：account_id 缺失/不存在同样在此拒绝（此前 dividend
    缺校验会落到 ledger FK 约束裸 500）。"""
    from app.model import Account
    acc = db.get(Account, account_id) if account_id else None
    if acc is None:
        raise ValueError(f"apply 需有效 account_id（收到 {account_id!r}）")
    if acc.status == "closed":
        raise ValueError(f"账户 #{account_id}（{acc.currency}）已于 {acc.closed_on} "
                         f"关池，只读终态不可记新流水（§6.6）")


def apply_buy(db: Session, *, entity_id: int, company: str, ticker: str | None = None,
              date, unit_price: float, shares: float, event_id: str, account_id: int) -> dict:
    """买入建仓：写 holding_event(batch) + ledger(现金移出，kind=investment)。

    买入瞬间总资产 = 现金 − 买价 + 持仓(+买价) = 净零，不重不漏。
    """
    if _event_nonce_applied(db, entity_id, event_id):
        return {"event_id": event_id, "entity_id": entity_id, "company": company,
                "batch_id": None, "shares": shares, "cost_basis": 0.0, "skipped": True}
    _require_open_account(db, account_id)   # 五轮审计 #176：关池拒新流水
    if not (account_id and unit_price and shares > 0):
        raise ValueError("apply_buy 需 account_id / unit_price>0 / shares>0")
    batch_id = next(_next_batch_ids(db, 1))
    db.add(HoldingEvent(entity_id=entity_id, company=company, ticker=ticker, date=date,
                        event_type="buy", batch_id=batch_id, shares=shares,
                        unit_price=unit_price, amount=shares * unit_price / 10000.0,
                        source_file=event_id))
    # kind='expense'（非 investment）：避免被 pool_in_transit（Σ kind in {investment,pool}）当作
    # 投资池在途，与持仓市值重复计数；净值由 stock_wealth（holding_event 市值）承载。
    db.add(LedgerEntry(account_id=account_id, date=date, reason=f"股票买入·{company}",
                       outflow=shares * unit_price, balance=None, kind="expense",
                       note=f"股票事件·{event_id}"))
    return {"event_id": event_id, "entity_id": entity_id, "company": company,
            "batch_id": batch_id, "shares": shares, "cost_basis": shares * unit_price,
            "skipped": False}


def apply_sell(db: Session, *, entity_id: int, company: str, date, shares: float,
               sell_price: float, event_id: str, account_id: int) -> dict:
    """FIFO 卖出：从最早 open batch 扣成本；写 sell 行 + ledger(本金 investment + 盈亏 investment_income)。

    超卖在写入前校验（422），避免中途改库后报错需回滚。非破坏式双写：减原 buy 行 shares（置 0 即结清）
    + 自留一条 sell 历史行，供日后全事件流 as-of 重放。
    """
    if _event_nonce_applied(db, entity_id, event_id):
        return {"event_id": event_id, "company": company, "sold_shares": shares,
                "cost_basis": 0.0, "proceeds": 0.0, "realized_pnl": 0.0,
                "accepted": [], "skipped": True}
    _require_open_account(db, account_id)   # 五轮审计 #176
    if not account_id or shares <= 0:
        raise ValueError("apply_sell 需 account_id / shares>0")
    rows = db.execute(select(HoldingEvent).where(
        HoldingEvent.entity_id == entity_id,
        HoldingEvent.company == company,
        HoldingEvent.shares > 0,
        HoldingEvent.event_type != "sell",
        HoldingEvent.event_type != "pseudo",
        HoldingEvent.closed_on.is_(None),
    ).order_by(HoldingEvent.date, HoldingEvent.id)).scalars().all()
    available = sum(float(r.shares) for r in rows)
    # 四轮审计 #169：浮点残差容差——部分卖出的 float 累减可致 available=…9999998，
    # 足额卖出被误判超卖；差值在 1e-6 相对量级内视为足额
    if shares > available and shares - available > max(1e-6, available * 1e-9):
        raise ValueError(f"卖出 {shares} 股超持仓，可卖 {available} 股")
    remaining = float(shares)
    cost_sold = 0.0
    accepted: list[tuple] = []
    for r in rows:
        if remaining <= 0:
            break
        take = min(float(r.shares), remaining)
        cost_sold += take * float(r.unit_price or 0.0)
        r.shares = float(r.shares) - take      # 置 0 = 结清，保留历史行
        accepted.append((r.batch_id, take))
        remaining -= take
    cost_sold = round(cost_sold, 6)
    proceeds = round(float(shares) * float(sell_price), 6)
    realized_pnl = round(proceeds - cost_sold, 6)
    ticker = rows[0].ticker if rows else None
    db.add(HoldingEvent(entity_id=entity_id, company=company, ticker=ticker, date=date,
                        event_type="sell", shares=float(shares), unit_price=cost_sold / shares,
                        amount=proceeds / 10000.0, source_file=event_id))
    # 本金归还现金（kind='income'，非 investment → pool_in_transit 不误判、不与市值重复计数）
    db.add(LedgerEntry(account_id=account_id, date=date, reason=f"股票卖出·{company}",
                       inflow=cost_sold, balance=None, kind="income",
                       note=f"股票事件·{event_id}"))
    # 盈亏（可负）→ investment_income；两笔合计 = 套现现金 proceeds
    if realized_pnl:
        db.add(LedgerEntry(account_id=account_id, date=date, reason=f"股票卖出盈亏·{company}",
                           inflow=realized_pnl if realized_pnl >= 0 else None,
                           outflow=-realized_pnl if realized_pnl < 0 else None,
                           balance=None, kind="investment_income", note=f"股票事件·{event_id}"))
    return {"event_id": event_id, "company": company, "sold_shares": float(shares),
            "cost_basis": cost_sold, "proceeds": proceeds, "realized_pnl": realized_pnl,
            "accepted": accepted, "skipped": False}


def apply_dividend(db: Session, *, entity_id: int, company: str, date, per_share: float,
                   event_id: str, account_id: int) -> dict:
    """分红结算：每股 × 加权现持仓 → ledger(income)，不写 holding_event（股数/成本不变）。"""
    existing = db.execute(select(LedgerEntry.id).where(
        LedgerEntry.note.like(f"%{event_id}%")).limit(1)).scalar_one_or_none()
    if existing is not None:
        return {"event_id": event_id, "company": company, "holding_shares": 0.0,
                "dividend": 0.0, "skipped": True}
    _require_open_account(db, account_id)   # 五轮审计 #176
    holding = sum(b["shares"] for b in _open_batches(db, entity_id, company))
    amount = round(holding * per_share, 6)
    db.add(LedgerEntry(account_id=account_id, date=date, reason=f"股票分红·{company}",
                       inflow=amount, balance=None, kind="investment_income",
                       note=f"股票事件·{event_id}"))
    return {"event_id": event_id, "company": company, "holding_shares": holding,
            "dividend": amount, "skipped": False}


def apply_passive_uplift(db: Session, *, entity_id: int, company: str, date, to_pct: float | None,
                         event_id: str, ticker: str | None = None) -> dict:
    """被动抬升（回购缩股本）：持股不变、无现金动作 → 仅写 pseudo 行更新 pct，不写 ledger。

    本轮实现语义 a（仅更新 pct，持股市值不变）；语义 b（回购缩股本按 to_shares 下调）
    涉及 unit_price 再平衡 + H-STOCK 联动，留后续（见函数 docstring）。
    """
    if _event_nonce_applied(db, entity_id, event_id):
        return {"event_id": event_id, "company": company, "event_type": "pseudo",
                "pct": to_pct, "skipped": True}
    if not _open_batches(db, entity_id, company):
        return {"event_id": event_id, "company": company, "event_type": "pseudo",
                "pct": to_pct, "skipped": True}   # 无持仓则无抬升对象
    # shares=0：该行仅作占比/历史标记，不构成 open 头寸（market_value_at 按 shares>0 排除，
    # 避免与原 buy 批次重复计数——被动抬升持股不变、市值不变）。
    db.add(HoldingEvent(entity_id=entity_id, company=company, ticker=ticker, date=date,
                        event_type="pseudo", batch_id=next(_next_batch_ids(db, 1)),
                        shares=0.0, unit_price=None, amount=None, pct=to_pct,
                        source_file=event_id))
    return {"event_id": event_id, "company": company, "event_type": "pseudo",
            "pct": to_pct, "skipped": False}