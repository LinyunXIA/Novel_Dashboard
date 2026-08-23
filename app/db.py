"""Novel Dashboard 数据库（DESIGN §4/§5）。

SQLAlchemy engine/session；Alembic 迁移入口。Base 供 ORM 模型继承。

设计要点：
- 默认 \`engine\` / \`SessionLocal\` 绑定到 \`APP_ENV\` 解析出的 DSN，供 FastAPI / alembic 等
  单进程单环境场景使用。
- 提供 \`make_engine(env)\` / \`make_sessionmaker(env)\` / \`check_connection_for(env)\`，
  按指定 env 显式重建连接——给 ingest CLI 等 \`--env\` 驱动场景使用，杜绝「打印 prod
  实际写入 dev」的脱节（issue #3）。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import CONFIG, get_config


# 默认连接：绑定到 APP_ENV 解析出的 DSN（FastAPI / alembic 用）
engine: Engine = create_engine(CONFIG.dsn, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    """全 ORM 模型基类（DESIGN §5 DDL 的 SQLAlchemy 表达，后续 F-P0-07 填充）。"""
    pass


def make_engine(env: Optional[str] = None) -> Engine:
    """按 env 显式构造 engine；不修改模块默认 engine（避免污染 FastAPI/alembic）。"""
    cfg = get_config(env)
    return create_engine(cfg.dsn, pool_pre_ping=True, future=True)


def make_sessionmaker(env: Optional[str] = None):
    """按 env 构造 sessionmaker；与 \`SessionLocal()\` 同接口，with-block 用法一致。"""
    eng = make_engine(env)
    return sessionmaker(bind=eng, autoflush=False, autocommit=False, future=True)


def check_connection_for(env: Optional[str] = None) -> bool:
    """连通性自检（ingest CLI 按 \`--env\` 自检）。"""
    eng = make_engine(env)
    with eng.connect() as conn:
        return conn.execute(text("SELECT 1")).scalar() == 1


def get_session():
    """依赖注入用 session 生成器（FastAPI/Script 均可挂靠）。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_connection() -> bool:
    """连通性自检（按 APP_ENV，默认连接）。"""
    with engine.connect() as conn:
        return conn.execute(text("SELECT 1")).scalar() == 1