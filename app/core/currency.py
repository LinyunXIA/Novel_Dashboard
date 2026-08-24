"""多币种折算（DESIGN §3 core/currency.py · issue #71 统一权威实现）。

usd_rate() 是全系统唯一的 currency→USD 折算入口（Decimal 口径）：
- 优先取该年具体汇率；缺则回退基准常量（year IS NULL）；
- 正向 USD→X 取倒数、反向 X→USD 直取，两分支对称；
- **缺汇率返回 None**：调用方必须跳过该币种贡献并显式告警，
  绝不静默 fallback 1.0（issue #2 根因，数值纪律）。

历史背景：snapshot.py / wealth.py 曾各持一份语义相同、精度不同的实现
（Decimal vs float），存在口径漂移风险；本模块合并后二者仅做薄适配。
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.model import ExchangeRate


def usd_rate(session: Session, currency: str, year: int) -> Decimal | None:
    """1 单位 currency 兑多少 USD；汇率缺失返回 None。"""
    if currency == "USD":
        return Decimal(1)
    # 正向：USD→<currency> 行 rate = 1 USD 兑 X currency → 取倒数
    row = session.execute(
        select(ExchangeRate.rate).where(
            ExchangeRate.fx_from == "USD", ExchangeRate.fx_to == currency,
            or_(ExchangeRate.year == year, ExchangeRate.year.is_(None)),
        )
        .order_by(ExchangeRate.year.is_(None), ExchangeRate.year.desc())
        .limit(1)
    ).first()
    if row is not None and row[0] is not None:
        return Decimal(1) / Decimal(row[0])
    # 反向：<currency>→USD 行直取（同样按 具体年 > 基准常量 优先）
    row2 = session.execute(
        select(ExchangeRate.rate).where(
            ExchangeRate.fx_from == currency, ExchangeRate.fx_to == "USD",
            or_(ExchangeRate.year == year, ExchangeRate.year.is_(None)),
        )
        .order_by(ExchangeRate.year.is_(None), ExchangeRate.year.desc())
        .limit(1)
    ).first()
    if row2 is not None and row2[0] is not None:
        return Decimal(row2[0])
    return None
