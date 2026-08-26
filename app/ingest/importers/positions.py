"""外部系统 API② 在岗岗位导入 + 用工成本落账（DESIGN §13.2 · F-P1-10）。

外部只导出在岗岗位明细（GET /public/positions，无任何成本字段）；我方用本地基准
（labor_cost.py）算成本 → 每公司×年 用工成本 → finance_entry(entity_kind='company',
kind='expense') → 增量重算（复用 _after_ui_write 路径）。

在岗判定（岗位/公司同规则）：opening ≤ Y-12-31 且 (closing 空 或 ≥ Y-01-01)；opening 空不计。
"""
from __future__ import annotations

from datetime import date

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.ingest.importers._client import _api_root, login, load
from app.core import labor_cost as LC
from app.model import Entity, FinanceEntry
from app.model.types import SourceKind


def login_and_token(base_url: str, username: str, password: str,
                    client: httpx.Client | None = None) -> str:
    return login(base_url, username, password, client=client)


def fetch_positions(base_url: str, token: str, year: int, company_ids: list[int] | None = None,
                    client: httpx.Client | None = None) -> list[dict]:
    """GET {api_root}/public/positions?year=Y[&company_ids=...] → items。"""
    owned = client is None
    client = client or httpx.Client(timeout=30)
    try:
        params = {"year": year}
        if company_ids:
            params["company_ids"] = ",".join(str(i) for i in company_ids)
        r = client.get(f"{_api_root(base_url)}/public/positions",
                       params=params,
                       headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return (r.json() or {}).get("items", [])
    finally:
        if owned:
            client.close()


def _in_post(rec: dict, year: int) -> bool:
    """在岗判定：opening ≤ Y-12-31 且 (closing 空 或 ≥ Y-01-01)；opening 空 → False。"""
    op = rec.get("opening_date")
    if not op:
        return False
    y_start = date(year, 1, 1)
    y_end = date(year, 12, 31)
    try:
        o = date.fromisoformat(op)
    except ValueError:
        return False
    if o > y_end:
        return False
    cl = rec.get("closing_date")
    if cl:
        try:
            c = date.fromisoformat(cl)
        except ValueError:
            c = None
        if c is not None and c < y_start:
            return False
    return True


def _resolve_entity(db: Session, company_id=None, company_name=None) -> int | None:
    """外部 company → 我方 entity(company).id：优先按名，退按 external_id(fields)。"""
    if company_name:
        ent = db.execute(select(Entity).where(
            Entity.entity_type == "company", Entity.name == company_name)).scalar_one_or_none()
        if ent is not None:
            return ent.id
    if company_id is not None:
        ent = db.execute(select(Entity).where(
            Entity.entity_type == "company",
            Entity.fields["external_id"].astext == str(company_id))).scalar_one_or_none()
        if ent is not None:
            return ent.id
    return None


def aggregate_to_finance(db: Session, costs: list[dict], year: int) -> dict:
    """逐岗位成本 → 按我方公司实体聚合 → 写 finance_entry(company, expense, 用工成本·{year})。

    幂等：先删同源/同公司/同年/「用工成本·」expense 再插入（同年重算=替换）。
    返回 {companies: 每公司条目, total_positions, skipped}。
    """
    by_company: dict[tuple[int, str], dict] = {}
    unresolved = 0
    for c in costs:
        if c is None or c.get("total") is None:
            continue
        eid = _resolve_entity(db, c.get("company_id"), c.get("company_name"))
        if eid is None:
            unresolved += 1
            continue
        key = (eid, c.get("currency"))
        d = by_company.setdefault(key, {"entity_id": eid, "company_id": c.get("company_id"),
                                        "company_name": c.get("company_name"),
                                        "currency": c.get("currency"),
                                        "total": 0.0, "positions": 0})
        d["total"] += c["total"]
        d["positions"] += 1

    ids = [k[0] for k in by_company]
    if ids:
        db.execute(delete(FinanceEntry).where(
            FinanceEntry.entity_id.in_(ids), FinanceEntry.year == year,
            FinanceEntry.kind == "expense", FinanceEntry.source == "external-api",
            FinanceEntry.label.like("用工成本·%")))
    out = []
    for (eid, cur), d in by_company.items():
        label = f"用工成本·{year}"
        db.add(FinanceEntry(entity_id=eid, entity_kind="company", year=year,
                            kind="expense", amount=round(d["total"], 2),
                            currency=cur, label=label, source=SourceKind.EXTERNAL_API))
        out.append({**d, "label": label})
    return {"companies": out, "total_positions": len(costs), "unresolved_company": unresolved}


def run_labor_cost(db: Session, year: int, company_ids: list[int] | None = None,
                   base_url: str | None = None, client: httpx.Client | None = None) -> dict:
    """端到端：登录→拉岗位→逐岗位成本→聚合落账（不 commit；由端点/命令层 commit+重算）。"""
    url, username, password = load(base_url=base_url)
    token = login_and_token(url, username, password, client=client)
    positions = fetch_positions(url, token, year, company_ids, client=client)
    costs = []
    unknown_levels: list[str] = []
    for p in positions:
        if not _in_post(p, year):
            continue
        lvl = p.get("level")
        if lvl and lvl not in LC.LEVEL_PCT:
            # 外部 API v2.6 /public/levels 字典对齐提示：未知 level 会按 0% 费率计成本
            # （静默低估）——计入返回统计供调用方察觉，必要时以 public.levels 对齐编码
            unknown_levels.append(str(lvl))
        c = LC.compute_position_cost(db, p, year)
        costs.append(c)
    agg = aggregate_to_finance(db, costs, year)
    return {"year": year, "companies_computed": agg, "positions_fetched": len(positions),
            "unknown_levels": sorted(set(unknown_levels))}


__all__ = ["run_labor_cost", "fetch_positions", "login_and_token",
           "aggregate_to_finance", "_in_post"]