"""Derived / reference tables (DESIGN §5.2): return_curve, fx, snapshot, timeline, jobs..."""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index,
    Integer, JSON, Numeric, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.db import Base


# issue #21：测试在 SQLite 上跑（无 JSONB / ARRAY），生产 Postgres 才用原生类型。
# JSONB → JSON：with_variant 标准模式（JSON 文本透明序列化）。
JSONBCompat = JSONB().with_variant(JSON(), "sqlite")


class ArrayTextCompat(TypeDecorator):
    """ARRAY(Text) 的 SQLite fallback：测试用 JSON 存 list[str]。

    Postgres 直通 ARRAY(Text)；SQLite 用 JSON 存 list 字符串，读回 list。
    """
    impl = ARRAY(Text)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(JSON())
        return dialect.type_descriptor(ARRAY(Text))

    def process_bind_param(self, value, dialect):
        if dialect.name == "sqlite" and value is not None and not isinstance(value, (str, bytes)):
            return list(value)
        return value

    def process_result_value(self, value, dialect):
        if dialect.name == "sqlite" and isinstance(value, str):
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return value
        return value


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
    overlay: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)   # issue #132
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
    payload: Mapped[dict] = mapped_column(JSONBCompat, nullable=False)
    # issue #21：原实现无 server_default 也无 Python default，INSERT 必炸 NOT NULL
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class IngestReport(Base):
    """导入前冲突检测 / 解析失败的持久化报告（issue #118 · §11.4）。

    此前冲突命中仅 stdout echo，进程结束即失；落库后供「导入状态」屏
    与数据调整员事后回看。level：block=硬拦截（文件未入库）/ warn=软警告
    （入库但高亮）/ error=解析失败。
    """
    __tablename__ = "ingest_report"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_path: Mapped[str] = mapped_column(Text, nullable=False, comment="源相对路径")
    rule: Mapped[Optional[str]] = mapped_column(Text, comment="规则名（H1/H2/H4/FX-AUTH…）；解析失败为 NULL")
    level: Mapped[str] = mapped_column(String, nullable=False,
                                       comment="block=硬拦截 / warn=软警告 / error=解析失败")
    line: Mapped[Optional[int]] = mapped_column(Integer, comment="源文件行号")
    detail: Mapped[str] = mapped_column(Text, nullable=False, comment="含新旧值对照的明细")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("level IN ('block','warn','error')", name="ck_ingest_report_level"),
        Index("ix_ingest_report_level_created", "level", "created_at"),
    )


class Snapshot(Base):
    __tablename__ = "snapshot"
    __table_args__ = (
        Index("ix_snap_years", "as_of_year"),
        # issue #108：partial index WHERE 必须是 sa.text() SQL 子句；
        # 误用类型构造器 Text() 会导致 create_all() 编译即崩（迁移侧写法正确，故 DB 未暴露）。
        Index("ux_snap_year", "as_of_year", "scope", unique=True, postgresql_where=text("as_of_date IS NULL")),
        Index("ux_snap_date", "as_of_date", "scope", unique=True, postgresql_where=text("as_of_date IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    as_of_year: Mapped[int] = mapped_column(Integer, nullable=False)
    as_of_date: Mapped[Optional[date]] = mapped_column(Date, comment="date 级游标；NULL=仅年聚合")
    # issue #12/§21.5：三段式 scope——'account:{id}:{cur}' / 'entity:{id}:{cur}' / 'family:total'
    scope: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[Optional[float]] = mapped_column(Numeric)
    currency: Mapped[Optional[str]] = mapped_column(String)


class SourceFileVersion(Base):
    __tablename__ = "source_file_version"
    __table_args__ = (UniqueConstraint("file_path", "version", name="uq_sfv_path_version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # issue #21：补 server_default，否则 raw SQL/COPY 插入遇 NOT NULL 失败
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    is_current: Mapped[Optional[bool]] = mapped_column(Boolean)


class RecomputeJob(Base):
    __tablename__ = "recompute_job"
    __table_args__ = (CheckConstraint("status IN ('pending','running','done','failed')", name="ck_recompute_status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    start_year: Mapped[Optional[int]] = mapped_column(Integer)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    # issue #21：DESIGN §5.2 DDL 为 TEXT[]，实现误用 JSON；改回 ARRAY(Text) 恢复数组语义
    files: Mapped[Optional[list[str]]] = mapped_column(ArrayTextCompat())
    status: Mapped[Optional[str]] = mapped_column(String, default="pending")
    # issue #21：补 server_default
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Notification(Base):
    __tablename__ = "notification"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("recompute_job.id"))
    kind: Mapped[Optional[str]] = mapped_column(String)
    title: Mapped[Optional[str]] = mapped_column(String)
    message: Mapped[Optional[str]] = mapped_column(Text)
    payload: Mapped[Optional[dict]] = mapped_column(JSONBCompat)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # issue #21：补 server_default
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


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
    locked: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    # issue #82：已赎回标记（防重 to 按笔而非按年；GET 据此暴露 redeemed 置灰）
    redeemed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class InvestmentAlloc(Base):
    __tablename__ = "investment_alloc"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    investment_id: Mapped[int] = mapped_column(ForeignKey("investment.id", ondelete="CASCADE"), nullable=False)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entity.id"), nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric, nullable=False)
    is_all: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)

    entity: Mapped["Entity"] = relationship()  # noqa: F821  (runtime imports resolve)


# forward import to satisfy .entity relationship type
from app.model.core import Entity  # noqa: E402  isort:skip

class ImportJob(Base):
    """外部导入异步任务（issue #138 · DESIGN §14.2 import-jobs）。

    provider ∈ {'company-info','labor-cost'}；payload 为该 provider 的请求参数
    （company-info: {}；labor-cost: {year, company_ids?}）。status 走与
    recompute_job 相同的 pending→running→done/failed 生命周期。
    """
    __tablename__ = "import_job"
    __table_args__ = (
        CheckConstraint("provider IN ('company-info','labor-cost')", name="ck_import_provider"),
        CheckConstraint("status IN ('pending','running','done','failed')", name="ck_import_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[Optional[dict]] = mapped_column(JSONBCompat, default=dict)
    status: Mapped[Optional[str]] = mapped_column(String, default="pending")
    result: Mapped[Optional[dict]] = mapped_column(JSONBCompat)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
