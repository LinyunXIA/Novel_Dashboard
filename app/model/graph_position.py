"""图谱节点布局坐标持久化（#201 · 拖拽后刷新锁定）。

按 (entity_id, scope) 存节点在 SVG viewBox 坐标系下的 x/y；scope ∈ {person, company, all}。
前端拖拽后 POST 覆盖；图谱响应含该 scope 的坐标，有则用（锁定），无则自动多层级布局。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class GraphNodePosition(Base):
    __tablename__ = "graph_node_position"
    __table_args__ = (UniqueConstraint("entity_id", "scope", name="uq_graph_pos_entity_scope"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entity.id"), nullable=False)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())