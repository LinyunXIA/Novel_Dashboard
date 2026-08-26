"""统一搜索索引（F-P1-08 · DESIGN §18）。

search_index：条目/行级 embedding（粒度 = 结构化条目标段，DESIGN §18.2）。
- source_table / source_row_id：定位源行（entity/timeline_event/finance_entry/…）
- content：该条目生成的中文描述句（含确定性数值，供 LLM 装配时原样引用）
- embedding vector(N)：pgvector（N=EMBED_DIM）；SQLite 测试降级 Text（对齐 JSONBCompat 先例）
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, DateTime, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.config import CONFIG
from app.db import Base

VectorCompat = Vector(CONFIG.embed_dim).with_variant(Text(), "sqlite")


class SearchIndex(Base):
    __tablename__ = "search_index"
    __table_args__ = (
        UniqueConstraint("source_table", "source_row_id", "content",
                         name="uq_search_source_row_content"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_table: Mapped[str] = mapped_column(Text, nullable=False)
    source_row_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding = mapped_column(VectorCompat, nullable=True, comment="pgvector 向量（EMBED_DIM）")
    # issue #132：与其他表统一 TIMESTAMPTZ（原 TIMESTAMP 无时区）
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now())