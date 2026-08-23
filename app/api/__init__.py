"""FastAPI 应用入口（F-P0-13）。`uvicorn app.api:app` 启动。"""
from .app import app

__all__ = ["app"]