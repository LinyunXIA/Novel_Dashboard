"""Derived / reference tables (DESIGN §5.2): return_curve, fx, snapshot, timeline, jobs..."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index,
    Integer, JSON, Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ReturnCurve(Base):
    __tablename__ = "return_curve"
    __table_args__ = (
        CheckConstraint("risk_lvl IN ('R1','R2','R3','R4','R5')", name="ck_return_risk"),
        UniqueConstraint("country", "risk_lvl", "year", name="uq_return_country_risk_year"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    country: Mapped[str] = mapped_column(String, nullable=False)
    risk_lvl: Mapped[str] = mapped_column(String, nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    rate: Mapped[Optional[float]] = mapped_column(Numeric, comment="年化收益 %（如 21.7）")
    source_file: Mapped[Optional[str]] = mapped_column(Text)
    source_line: Mapped[Optional[int]] = mapped_column(Integer)
    version_id: Mapped[Optional[int]] = mapped_column(BigInteger)


class ExchangeRate(Base):
    __tablename__ = "exchange_rate"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    fx_from: Mapped[str] = mapped_column(String, nullable=False)
    fx_to: Mapped[str] = mapped_column(String, nullable=False)
    year: Mapped[Optional[int]] = mapped_column(Integer, comment="NULL=基准折算率常量")
    rate: Mapped[Optional[float]] = mapped_column(Numeric)
    source_file: Mapped[Optional[str]] = mapped_column(Text)
    source_line: Mapped[Optional[int]] = mapped_column(Integer)
    version_id: Mapped[Optional[int]] = mapped_column(BigInteger)


class DateRule(Base):
    __tablename__ = "date_rule"
    __table_args__ = (UniqueConstraint("pattern", name="uq_date_rule_pattern"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pattern: Mapped[str] = mapped_column(String, nullable=False, comment="year-only / year-month / 上旬 / 中旬 / 下旬 / ...")
    resolve: Mapped[str] = mapped_column(String, nullable=False, comment="12-30 / 月底 / 1日 / ...")
    note: Mapped[Optional[str]] = mapped_column(Text)


class TimelineEvent(Base):
    __tablename__ = "timeline_event"
    __table_args__ = (Index("ix_timeline_year", "event_year"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_year: Mapped[int] = mapped_column(Integer, nullable=False)
    event_date: Mapped[Optional[date]] = mapped_column(Date)
    title: Mapped[str] = mapped_column(String, nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text)
    decade: Mapped[Optional[str]] = mapped_column(String)
    overlay: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source_file: Mapped[Optional[str]] = mapped_column(Text)
    source_line: Mapped[Optional[int]] = mapped_column(Integer)
    version_id: Mapped[Optional[int]] = mapped_column(BigInteger)


class Relationship(Base):
    __tablename__ = "relationship"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    from_entity_id: Mapped[int] = mapped_column(ForeignKey("entity.id"), nullable=False)
    to_entity_id: Mapped[int] = mapped_column(ForeignKey("entity.id"), nullable=False)
    rel_type: Mapped[str] = mapped_column(String, nullable=False, comment="parent/child/member/holds/acquired/split")
    since_year: Mapped[Optional[int]] = mapped_column(Integer)
    until_year: Mapped[Optional[int]] = mapped_column(Integer)
    source_file: Mapped[Optional[str]] = mapped_column(Text)
    source_line: Mapped[Optional[int]] = mapped_column(Integer)
    version_id: Mapped[Optional[int]] = mapped_column(BigInteger)


class UserDataOverlay(Base):
    __tablename__ = "user_data_overlay"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    section: Mapped[str] = mapped_column(String, nullable=False, comment="'timeline' 等")
    key: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Snapshot(Base):
    __tablename__ = "snapshot"
    __table_args__ = (
        Index("ix_snap_years", "as_of_year"),
        Index("ux_snap_year", "as_of_year", "scope", unique=True, postgresql_where=(Text("as_of_date IS NULL"))),
        Index("ux_snap_date", "as_of_date", "scope", unique=True, postgresql_where=(Text("as_of_date IS NOT NULL"))),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    as_of_year: Mapped[int] = mapped_column(Integer, nullable=False)
    as_of_date: Mapped[Optional[date]] = mapped_column(Date, comment="date 级游标；NULL=仅年聚合")
    scope: Mapped[str] = mapped_column(String, nullable=False, comment="'account:12:BEF' / 'entity:3' / 'family:total'")
    value: Mapped[Optional[float]] = mapped_column(Numeric)
    currency: Mapped[Optional[str]] = mapped_column(String)


class SourceFileVersion(Base):
    __tablename__ = "source_file_version"
    __table_args__ = (UniqueConstraint("file_path", "version", name="uq_sfv_path_version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_current: Mapped[Optional[bool]] = mapped_column(Boolean)


class RecomputeJob(Base):
    __tablename__ = "recompute_job"
    __table_args__ = (CheckConstraint("status IN ('pending','running','done','failed')", name="ck_recompute_status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    start_year: Mapped[Optional[int]] = mapped_column(Integer)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    files: Mapped[Optional[list]] = mapped_column(JSON)
    status: Mapped[Optional[str]] = mapped_column(String, default="pending")
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Notification(Base):
    __tablename__ = "notification"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("recompute_job.id"))
    kind: Mapped[Optional[str]] = mapped_column(String)
    title: Mapped[Optional[str]] = mapped_column(String)
    message: Mapped[Optional[str]] = mapped_column(Text)
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Investment(Base):
    __tablename__ = "investment"
    __table_args__ = (
        CheckConstraint("risk_lvl IN ('R1','R2','R3','R4','R5')", name="ck_investment_risk"),
        UniqueConstraint("year", "region", name="uq_investment_year_region"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    region: Mapped[str] = mapped_column(String, nullable=False, comment="欧洲/英国/美国/香港/中国")
    risk_lvl: Mapped[str] = mapped_column(String, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    locked: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class InvestmentAlloc(Base):
    __tablename__ = "investment_alloc"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    investment_id: Mapped[int] = mapped_column(ForeignKey("investment.id", ondelete="CASCADE"), nullable=False)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entity.id"), nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric, nullable=False)
    is_all: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    entity: Mapped["Entity"] = relationship()  # noqa: F821  (runtime imports resolve)


# forward import to satisfy .entity relationship type
from app.model.core import Entity  # noqa: E402  isort:skip