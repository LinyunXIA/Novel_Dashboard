"""FastAPI 依赖：session per request。"""
from __future__ import annotations

from typing import Generator

from app.db import SessionLocal


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()