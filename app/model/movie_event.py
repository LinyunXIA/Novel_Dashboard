"""事件·电影（F-P2-01 · DESIGN §19.6）。

电影事件导入 + 不关联 + UI 同币种手动关联 → 写 ledger。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.model.core import JSONBCompat


class MovieEvent(Base):
    __tablename__ = "movie_event"
    __table_args__ = (UniqueConstraint("title", "source_file", name="uq_movie_title_source"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    currency: Mapped[Optional[str]] = mapped_column(String, comment="默认 USD")
    region: Mapped[Optional[str]] = mapped_column(String, comment="NA/OS/single")
    investment_total: Mapped[Optional[float]] = mapped_column(Numeric)
    investment_date: Mapped[Optional[date]] = mapped_column(Date)
    principal_return_date: Mapped[Optional[date]] = mapped_column(Date)
    principal_return_amount: Mapped[Optional[float]] = mapped_column(Numeric)
    dividends_total: Mapped[Optional[float]] = mapped_column(Numeric)
    raw_cashflows: Mapped[Optional[dict]] = mapped_column(JSONBCompat,
                                                         comment="解析出的全部现金流明细")
    linked_account_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("account.id"))
    linked_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    source_file: Mapped[Optional[str]] = mapped_column(Text)
    source_line: Mapped[Optional[int]] = mapped_column(Integer)
    version_id: Mapped[Optional[int]] = mapped_column(BigInteger)