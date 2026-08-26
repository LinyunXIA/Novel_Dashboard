"""划拨 / 换汇（DESIGN §19.5 · F-P1-03）。

UI 第二类改数据操作（§6.8）：输入 年份 → 源账户 → 源币资金池 → 目标币种。
- 同币种 = 划拨：源账户记支出 + 目标账户记收入，两笔金额相等（净额 0），可跨实体。
- 跨币种 = 换汇：需 `exchange_rate` **该年份** 汇率；目标金额 = 源金额 × rate(year)。
- 记年：锁输入年份，两笔 ledger 落 year-12-30；后传重算。
- 校验：源账户自 year 起向后全链 as-of 不得为负，任一年拐负 → 整体拒绝（409/422）。

数值纪律（对齐 §19.3 覆盖连锁 / §19.4 复用向后传播校验）：全程 Decimal。
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import CALENDAR_MAX_YEAR
from app.core.invest import ValidationError
from app.model import (
    Account, ExchangeRate, LedgerEntry, TimelineEvent,
)

_ZERO = Decimal(0)
_MAX_YEAR = CALENDAR_MAX_YEAR  # issue #141：日历年上限收敛 config 单一来源


def primary_account(session: Session, entity_id: int, currency: str) -> Optional[Account]:
    """该主体该币种记账账户（与 invest 同口径；取最小 id active 账户）。"""
    return session.execute(
        select(Account).where(Account.entity_id == entity_id,
                              Account.currency == currency,
                              Account.status == "active")
        .order_by(Account.id).limit(1)
    ).scalar_one_or_none()


def _fx_rate(session: Session, fx_from: str, fx_to: str, year: int) -> Optional[Decimal]:
    """该年直接货币对汇率（§19.5：换汇命中该年汇率才可换，不做全时段穷举）。

    只取该年具体行（year==year，不含 year IS NULL 基准常量）。
    issue #87-1：缺正向方向时取反向行（fx_to→fx_from）取倒数——来源币向通常只存单向，
    EUR↔BEF 应可双向成交（倒数是精确的数学逆）。
    """
    row = session.execute(
        select(ExchangeRate.rate).where(
            ExchangeRate.fx_from == fx_from,
            ExchangeRate.fx_to == fx_to,
            ExchangeRate.year == year,
        ).limit(1)
    ).first()
    if row is not None and row[0] is not None:
        rate = Decimal(str(row[0]))
        if rate > 0:                     # issue #161：正向行同样拒绝 0/负值（防 amt×0 资金蒸发）
            return rate
        return None                      # 非法汇率行视同缺失 → 上层 422
    rrow = session.execute(
        select(ExchangeRate.rate).where(
            ExchangeRate.fx_from == fx_to,
            ExchangeRate.fx_to == fx_from,
            ExchangeRate.year == year,
        ).limit(1)
    ).first()
    if rrow is not None and rrow[0] is not None:
        rate = Decimal(str(rrow[0]))
        if rate > 0:                     # 五轮审计 #175：与正向对称，负值取倒数仍非法
            return Decimal(1) / rate
    return None


def available_fx_pairs(session: Session, year: int) -> list[tuple[str, str]]:
    """该年在库的**有效**直接货币对方向（issue #87-1：供错误提示/前端可用方向下拉）。

    五轮审计 #175：0/负汇率行不可用于换汇（_fx_rate 拒绝），提示列表同步过滤，
    避免展示可选但提交必 422 的方向。
    """
    return session.execute(
        select(ExchangeRate.fx_from, ExchangeRate.fx_to)
        .where(ExchangeRate.year == year,
               ExchangeRate.rate.isnot(None), ExchangeRate.rate > 0)
        .order_by(ExchangeRate.fx_from, ExchangeRate.fx_to)
        .distinct()
    ).all()


def _simulate_annual_asof_nonneg(session: Session, account_id: int,
                                 amount: Decimal, from_year: int) -> bool:
    """转出向后全链 as-of 不破负（§19.5 gate）：源账户自 from_year 起逐年 as-of 滚动，
    并叠加在 from_year 的一笔 amount 支出；任一年年末（12-30 口径）为负 → False。

    与 snapshot._account_balance_series 同源逐年口径；复用向后传播校验（§19.4）。
    """
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
    # 基线：from_year 之前（含所有历史年）的累计 as-of，保证 from_year 起能承接历史余额
    bal: Decimal = _ZERO
    for y in sorted(set(year_in) | set(year_out)):
        if y >= from_year:
            break
        bal += year_in.get(y, _ZERO) - year_out.get(y, _ZERO)
    for y in range(from_year, _MAX_YEAR + 1):
        bal = bal + year_in.get(y, _ZERO) - year_out.get(y, _ZERO)
        if y == from_year:
            bal -= amount
        if bal < _ZERO:
            return False
    return True


def transfer(session: Session, *, source_account_id: int, target_entity_id: int,
             target_currency: str, amount, year: int,
             nonce: str | None = None) -> dict:
    """划拨（同币）/ 换汇（跨币）。返回 {operation, source_account_id,
    target_account_id, source_currency, target_currency, amount, target_amount, year,
    skipped}。

    校验：金额>0；源/目标账户存在；源非关池（§6.6 只读终态，issue #83）；源自 year 起全链
    as-of 非负（否则拒绝）；换汇需该年汇率。成功后：写两笔 ledger（year-12-30）→ 编年史 overlay。

    七轮审计 #182：nonce 幂等——API 层生成唯一 nonce，两笔 ledger note 打
    `UI 转移#{nonce}` 标签；重放（同 nonce 已入账）→ skipped=True 不再写账，
    对齐 inv#/demand#/股票事件 nonce 三个既有先例。
    调用方负责 flush/commit + recompute_all + rebuild_snapshots + record_recompute_done。
    """
    source = session.get(Account, source_account_id)
    if not source:
        raise ValidationError(f"源账户 #{source_account_id} 不存在")
    if source.status == "closed":
        # §6.6：关池后 BEF/LUF/NLG 进入只读终态，不可再存/投/换汇——历史资金已全额
        # 结转 EUR 承接池，再从旧池转出会造成双份资金或污染 H4 余额链。
        raise ValidationError(
            f"源账户 #{source.id}（{source.currency}）已于 {source.closed_on} 关池进入只读终态，不可转出")
    target = primary_account(session, target_entity_id, target_currency)
    if not target:
        raise ValidationError(f"目标主体 #{target_entity_id} 无 {target_currency} 账户")
    amt = Decimal(str(amount))
    if amt <= _ZERO:
        raise ValidationError(f"划拨金额必须 > 0，得到 {amount}")

    at_date = date(year, 12, 30)
    same_currency = source.currency == target.currency

    # 七轮审计 #182：nonce 幂等重放（LIKE 粗筛 + Python 词边界精确复核，防 #1 命中 #10~19；
    # 与 invest.delete_derived_by_tag 同款手法，兼容 SQLite 测试环境）
    if nonce:
        tag = f"UI 转移#{nonce}"
        candidates = session.execute(
            select(LedgerEntry.id, LedgerEntry.note).where(
                LedgerEntry.note.like(f"%{tag}%"))
        ).all()
        if any(re.search(rf"{re.escape(tag)}(?!\d)", note or "") for _i, note in candidates):
            return {"operation": "重放跳过", "source_account_id": source.id,
                    "target_account_id": None, "source_currency": source.currency,
                    "target_currency": target_currency, "amount": float(amt),
                    "target_amount": 0.0, "year": year, "skipped": True}

    # gate：源向后全链 as-of 不破负
    if not _simulate_annual_asof_nonneg(session, source.id, amt, year):
        raise ValidationError(
            f"源账户 #{source.id} 自 {year} 起全链 as-of 将拐负，整体拒绝转出")

    if same_currency:
        target_amount = amt
        op = "划拨"
        reason_src = f"划拨 {amt} {source.currency} → #{target_entity_id}"
        reason_tgt = f"收到划拨 {amt} {target.currency}（源 #{source.entity_id}）"
    else:
        rate = _fx_rate(session, source.currency, target.currency, year)
        if rate is None:
            pairs = available_fx_pairs(session, year)
            hint = ("；该年可用方向：" + "、".join(f"{f}→{t}" for f, t in pairs)
                    ) if pairs else "；该年无任何汇率行（缺 year-12-30 单，请数据调整员先导入）"
            raise ValidationError(
                f"缺 {year} 年 {source.currency}→{target.currency} 汇率，无法换汇{hint}")
        target_amount = (amt * rate).quantize(Decimal("0.01"))
        op = "换汇"
        reason_src = f"换汇 {amt} {source.currency} → {target.currency} @{year}"
        reason_tgt = f"换汇入账 {target_amount} {target.currency}（@{year} 汇率 {rate}）"

    tag = f"UI 转移#{nonce}" if nonce else f"UI 转移·{op}"
    session.add(LedgerEntry(
        account_id=source.id, date=at_date, reason=reason_src,
        outflow=amt, kind=None, note=f"{tag}·{op}·源",     ))
    session.add(LedgerEntry(
        account_id=target.id, date=at_date, reason=reason_tgt,
        inflow=target_amount, kind=None, note=f"{tag}·{op}·目标",     ))
    session.add(TimelineEvent(
        event_year=year, event_date=at_date,
        title=f"{op} {amt} {source.currency} → {target_amount} {target.currency}",
        note=(f"源 #{source.entity_id} → 目标 #{target_entity_id}"
              if not same_currency else
              f"划拨 源 #{source.entity_id} → 目标 #{target_entity_id}")
             + "（UI 转移）",   # issue #132：统一 §19 定位标签为「UI 转移」前缀
        decade=f"{year // 10 * 10}s", overlay=True,     ))
    return {
        "operation": op, "skipped": False,
        "source_account_id": source.id, "target_account_id": target.id,
        "source_currency": source.currency, "target_currency": target.currency,
        "amount": float(amt), "target_amount": float(target_amount), "year": year,
    }