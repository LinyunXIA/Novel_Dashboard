"""用工成本 API（API② · F-P1-10；DESIGN §13.2）。

- POST /labor-cost/compute    拉取岗位→算成本→写每公司 finance_entry→commit（UI 按钮触发）
- GET  /labor-cost/rules      加薪规则（Level/外包/晋级/CPI，纯展示）
- GET  /labor-cost/results    每公司×年用工成本只读表（从 finance_entry 读）

税率公式细节只在后台（labor_cost.py），本层不暴露费率。
本通道是 UI 用户触发的计算（F-U7 式），不挂 require_importer。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core import labor_cost as LC
from app.ingest.importers.positions import run_labor_cost
from app.model import Entity, FinanceEntry

router = APIRouter(prefix="/api/v1", tags=["labor-cost"])


class LaborCostIn(BaseModel):
    year: int  # 1900..2999
    company_ids: Optional[list[int]] = None


@router.post("/labor-cost/compute")
def compute(body: LaborCostIn, db: Session = Depends(get_db)):
    """拉取外部岗位→逐岗位算成本→聚合写每公司 finance_entry(expense)→commit。

    返回 {year, companies_computed, positions_fetched}。外部不通/4xx → 502/透传，不落库。
    """
    import httpx
    try:
        res = run_labor_cost(db, body.year, body.company_ids)
        db.commit()
    except httpx.HTTPStatusError as e:
        upstream = e.response.status_code if e.response is not None else None
        # issue #127：上游状态码不透传；凭据/权限类 → 503，其余 → 502，detail 附上游码
        mapped = 503 if upstream in (401, 403) else 502
        raise HTTPException(status_code=mapped,
                            detail=f"外部系统 API 错误（upstream HTTP {upstream}）")
    except (httpx.RequestError, httpx.TimeoutException):
        raise HTTPException(status_code=502, detail="无法连接外部系统 API（请确认其已启动且已导入公司 API①）")
    return res


@router.get("/labor-cost/rules")
def rules():
    """加薪规则（UI「加薪规则」屏数据源，单源自 labor_cost.RULES）。"""
    return LC.rules_payload()


@router.get("/labor-cost/results")
def results(year: Optional[int] = None, db: Session = Depends(get_db)):
    """每公司×年用工成本（从 finance_entry 读；source='external-api'，label=用工成本·{year}）。"""
    q = (select(FinanceEntry, Entity.name)
         .join(Entity, Entity.id == FinanceEntry.entity_id)
         .where(FinanceEntry.kind == "expense", FinanceEntry.source == "external-api",
                FinanceEntry.label.like("用工成本·%")))
    if year:
        q = q.where(FinanceEntry.year == year)
    rows = db.execute(q.order_by(FinanceEntry.year, Entity.name)).all()
    return {"items": [
        {"year": fe.year, "company_id": fe.entity_id, "company_name": name,
         "currency": fe.currency, "amount": float(fe.amount) if fe.amount is not None else None}
        for fe, name in rows]}