"""事件·股票（F-P2-02 · DESIGN §19.6）。

股票事件导入 + 不关联账户 → UI 同币种手动关联 → apply_buy/sell/dividend 实体化
holding_event(batch) + 写 ledger。一行 = 一个解析出的 Style A 事件。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class StockEvent(Base):
    __tablename__ = "stock_event"
    __table_args__ = (
        UniqueConstraint("company", "date", "event_type", "source_file",
                         name="uq_stock_company_date_type_source"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company: Mapped[str] = mapped_column(String, nullable=False)
    ticker: Mapped[Optional[str]] = mapped_column(String)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    event_type: Mapped[Optional[str]] = mapped_column(String,
                                                       comment="buy/sell/dividend/pseudo")
    currency: Mapped[Optional[str]] = mapped_column(String, comment="默认 USD")
    shares: Mapped[Optional[float]] = mapped_column(Numeric)
    unit_price: Mapped[Optional[float]] = mapped_column(Numeric)
    amount: Mapped[Optional[float]] = mapped_column(Numeric, comment="单位：万美金")
    pct: Mapped[Optional[float]] = mapped_column(Numeric)
    linked_entity_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("entity.id"))
    linked_account_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("account.id"))
    linked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))  # issue #145：统一 TIMESTAMPTZ
    source_file: Mapped[Optional[str]] = mapped_column(Text)
    source_line: Mapped[Optional[int]] = mapped_column(Integer)
    version_id: Mapped[Optional[int]] = mapped_column(BigInteger)