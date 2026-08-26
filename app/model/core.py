"""Core Phase-1 tables (DESIGN §5.2). SQLAlchemy 2.0 typed mapping."""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import (
    JSON, BigInteger, Boolean, CheckConstraint, Date, ForeignKey, Index,
    Integer, Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.model.types import AccountStatus, EntityType, LedgerKind, SourceKind, StreamType

# issue #21：测试在 SQLite 上跑（无 JSONB），生产 Postgres 才用 JSONB。with_variant 是
# SQLAlchemy 标准 fallback 模式——同一 Python 类型在两个 dialect 都可读写。
JSONBCompat = JSONB().with_variant(JSON(), "sqlite")


class Entity(Base):
    __tablename__ = "entity"
    __table_args__ = (
        CheckConstraint("entity_type IN ('person','company','asset','family')", name="ck_entity_type"),
        UniqueConstraint("entity_type", "name", name="uq_entity_type_name"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String)
    status: Mapped[Optional[str]] = mapped_column(String, comment="公司状态（仅 company；只增不减）")
    fields: Mapped[dict] = mapped_column(JSONBCompat, default=dict, nullable=False, server_default="{}")
    source_file: Mapped[Optional[str]] = mapped_column(Text)
    source_line: Mapped[Optional[int]] = mapped_column(Integer)
    source: Mapped[Optional[str]] = mapped_column(
        String, default=SourceKind.FILE.value, server_default="'file'")   # issue #132
    version_id: Mapped[Optional[int]] = mapped_column(BigInteger)


class Account(Base):
    __tablename__ = "account"
    __table_args__ = (
        CheckConstraint("status IN ('active','closed')", name="ck_account_status"),
        UniqueConstraint("entity_id", "currency", "bank", name="uq_account_entity_currency_bank"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entity.id"), nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, default=AccountStatus.ACTIVE.value, server_default="'active'", nullable=False)   # issue #132
    closed_on: Mapped[Optional[date]] = mapped_column(Date)
    migrate_to_currency: Mapped[Optional[str]] = mapped_column(String)
    bank: Mapped[Optional[str]] = mapped_column(String)

    entity: Mapped[Entity] = relationship()


class InitialAsset(Base):
    __tablename__ = "initial_asset"
    __table_args__ = (
        CheckConstraint("asset_type IN ('cash','bond','stock','property')", name="ck_initial_asset_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entity.id"), nullable=False)
    asset_type: Mapped[str] = mapped_column(String, nullable=False)
    group_key: Mapped[Optional[str]] = mapped_column(String, comment="股票债券同域打包颗粒（如 '丹麦股票债券'）")
    currency: Mapped[Optional[str]] = mapped_column(String)
    name: Mapped[Optional[str]] = mapped_column(String)
    face_value: Mapped[Optional[float]] = mapped_column(Numeric)
    pct: Mapped[Optional[float]] = mapped_column(Numeric)
    source_file: Mapped[Optional[str]] = mapped_column(Text)
    source_line: Mapped[Optional[int]] = mapped_column(Integer)

    entity: Mapped[Entity] = relationship()


class IncomeStream(Base):
    __tablename__ = "income_stream"
    __table_args__ = (
        CheckConstraint("stream_type IN ('rent','property','security','shop','salary')", name="ck_income_stream_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entity.id"), nullable=False)
    stream_type: Mapped[str] = mapped_column(String, nullable=False)
    group_key: Mapped[Optional[str]] = mapped_column(String, comment="属地/地域颗粒")
    currency: Mapped[str] = mapped_column(String, nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric, nullable=False, comment="文件写死的金额/税后值，直接入账")
    label: Mapped[Optional[str]] = mapped_column(String)
    source_file: Mapped[Optional[str]] = mapped_column(Text)
    source_line: Mapped[Optional[int]] = mapped_column(Integer)

    entity: Mapped[Entity] = relationship()


class LedgerEntry(Base):
    __tablename__ = "ledger_entry"
    __table_args__ = (
        CheckConstraint("kind IN ('income','expense','investment','investment_income','pool')", name="ck_ledger_kind"),
        Index("ix_ledger_acct_date", "account_id", "date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    inflow: Mapped[Optional[float]] = mapped_column(Numeric)
    outflow: Mapped[Optional[float]] = mapped_column(Numeric)
    balance: Mapped[Optional[float]] = mapped_column(Numeric, comment="余额（schema 存源值；重算校验连续）")
    kind: Mapped[Optional[str]] = mapped_column(String)
    note: Mapped[Optional[str]] = mapped_column(Text)
    source_file: Mapped[Optional[str]] = mapped_column(Text)
    source_line: Mapped[Optional[int]] = mapped_column(Integer)
    version_id: Mapped[Optional[int]] = mapped_column(BigInteger)

    account: Mapped[Account] = relationship()


class FinanceEntry(Base):
    __tablename__ = "finance_entry"
    __table_args__ = (
        CheckConstraint("kind IN ('income','expense','investment','investment_income','pool')", name="ck_finance_kind"),
        CheckConstraint("entity_kind IN ('person','company')", name="ck_finance_entity_kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entity.id"), nullable=False)
    entity_kind: Mapped[str] = mapped_column(String)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[Optional[float]] = mapped_column(Numeric)
    currency: Mapped[Optional[str]] = mapped_column(String)
    label: Mapped[Optional[str]] = mapped_column(String)
    source: Mapped[str] = mapped_column(String, default=SourceKind.FILE.value)
    source_file: Mapped[Optional[str]] = mapped_column(Text)
    source_line: Mapped[Optional[int]] = mapped_column(Integer)
    version_id: Mapped[Optional[int]] = mapped_column(BigInteger)

    entity: Mapped[Entity] = relationship()


class HoldingEvent(Base):
    __tablename__ = "holding_event"
    __table_args__ = (
        Index("ix_holding_entity", "entity_id", "company"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entity.id"), nullable=False)
    company: Mapped[str] = mapped_column(String, nullable=False)
    ticker: Mapped[Optional[str]] = mapped_column(String)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    event_type: Mapped[Optional[str]] = mapped_column(String, comment="buy/sell/split/acquire-cash/acquire-share/pseudo")
    batch_id: Mapped[Optional[int]] = mapped_column(BigInteger, comment="成本批次；FIFO 卖出扣成本")
    shares: Mapped[Optional[float]] = mapped_column(Numeric)
    unit_price: Mapped[Optional[float]] = mapped_column(Numeric, comment="该批次成本价（FIFO）")
    amount: Mapped[Optional[float]] = mapped_column(Numeric, comment="单位：万美金")
    pct: Mapped[Optional[float]] = mapped_column(Numeric)
    closed_on: Mapped[Optional[date]] = mapped_column(Date, comment="结清日；NULL=未结清(open)")
    source_file: Mapped[Optional[str]] = mapped_column(Text)
    source_line: Mapped[Optional[int]] = mapped_column(Integer)
    version_id: Mapped[Optional[int]] = mapped_column(BigInteger)

    entity: Mapped[Entity] = relationship()