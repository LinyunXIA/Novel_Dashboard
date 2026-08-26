"""FastAPI 依赖：session per request + 受限写通道守卫（DESIGN §14.1）。

写端点授权：除 timeline-events（编年史，经覆盖层）、source-files versions（diff 决策）
与 UI 派生通道（投资/划拨等 §19，前端友好）外，其余写端点（entities/ledger/finance
创建/改删）**不面向普通 UI 用户**，仅供 importer / 数据调整员。

本地单机无鉴权（PRD §13 非安全边界），用 header `X-Importer: 1` 作受限通道闸门：
- 缺该 header 的非中列端 → 403；
- UI 派生端点不挂本依赖，天然放行。
"""
from __future__ import annotations

from typing import Generator

from fastapi import Header, HTTPException

from app.db import SessionLocal


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_importer(x_importer: str | None = Header(default=None)) -> None:
    """受限写通道守卫：普通 UI 调用受限写端点应被拒绝（403）。

    本地开发降级：携带 `X-Importer: 1` 即视为数据调整员通道放行。
    """
    if x_importer != "1":
        raise HTTPException(
            status_code=403,
            detail="该写端点仅供 importer/数据调整员受限通道，普通 UI 无权调用（§14.1）",
        )

def apply_error(e: Exception):
    """UI 派生业务错误 → HTTPException（422/409 per status；issue #132 收敛重复定义）。"""
    from fastapi import HTTPException
    status = getattr(e, "status", 422)
    raise HTTPException(status_code=status, detail=getattr(e, "detail", str(e)))
