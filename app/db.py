"""Novel Dashboard 数据库（DESIGN §4/§5）。

SQLAlchemy engine/session；Alembic 迁移入口。Base 供 ORM 模型继承。
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import CONFIG

engine = create_engine(CONFIG.dsn, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    """全 ORM 模型基类（DESIGN §5 DDL 的 SQLAlchemy 表达，后续 F-P0-07 填充）。"""
    pass


def get_session():
    """依赖注入用 session 生成器（FastAPI/Script 均可挂靠）。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_connection() -> bool:
    """连通性自检（ingest CLI / 冒烟用）。"""
    with engine.connect() as conn:
        return conn.execute(__import__("sqlalchemy").text("SELECT 1")).scalar() == 1