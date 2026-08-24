"""统一搜索端点（F-P1-08 · DESIGN §18.4）。

GET /api/v1/search?q=<问题>[&as_of=<date>] → {answer, hits[]}。
omlx 不可用/未启动 → 503 带明确提示（让用户先起 omlx 或判定降级）。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.llm import LlmUnavailable
from app.search.search import search

router = APIRouter(prefix="/api/v1", tags=["search"])


@router.get("/search")
def search_endpoint(q: str = Query(..., description="问题"),
                    as_of: Optional[str] = Query(None, description="全局日历游标（预留）"),
                    db: Session = Depends(get_db)):
    try:
        return search(db, q, as_of)
    except LlmUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))