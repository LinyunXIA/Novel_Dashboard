"""外部系统 API① 公司基础信息导入（DESIGN §13.1/§13.3 · F-P1-05）。

GET /public/companies（JWT）→ 隶属公司 + 股权结构（internal/external company / person 股东
+ 持股比 + 状态 + 开停业日期）→ 按「只增不减」幂等 upsert 进 entity/relationship：

- 每个公司 → `entity(entity_type='company', name)` upsert，`source='external-api'`，`status` 映射
  （opened/closed），开停业日期/外部ID/持股比落 `fields` JSONB（Entity 无对应列，不开迁移）。
- 每个 shareholder → owner 实体（公司→company，自然人→person，miss 则 upsert）+ 指向本公司的
  `relationship(rel_type='holds')`，有效期窗口 since/until_year 取公司开停业年份。

只增不减：全部走 writer.upsert_entity / upsert_relationship（幂等，永不 DELETE）。
触发：公司图谱页「获取/导入公司」按钮 → POST /api/v1/graph/companies/import（F-U7）。
"""
from __future__ import annotations

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingest import writer
from app.ingest.importers._client import _api_root, load, login
from app.model import Entity


def _year(d: str | None) -> int | None:
    """'YYYY-MM-DD' → 年份；空/非法 → None。"""
    if not d:
        return None
    try:
        return int(str(d)[:4])
    except (TypeError, ValueError):
        return None


def fetch_companies(url: str, token: str, client: httpx.Client | None = None) -> list[dict]:
    """GET {api_root}/public/companies（Bearer）→ 隶属公司数组。"""
    owned = client is None
    client = client or httpx.Client(timeout=30)
    try:
        r = client.get(f"{_api_root(url)}/public/companies",
                       headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.json()
    finally:
        if owned:
            client.close()


def _shareholder_owner(session: Session, sh: dict) -> tuple[str, int, bool]:
    """从 shareholder 行解析 owner 实体：

    - internal_company_name / external_company_name → company 实体（公司股东）
    - person_name → person 实体（自然人股东，miss 则新建）
    返回 (owner_type, owner_id, created)。created 表示本次是否新建该实体。
    三者互斥；都空 → 返回 (None, None, False)。"""
    owner = None
    otype = "company"
    if sh.get("internal_company_name"):
        owner = sh["internal_company_name"]
    elif sh.get("external_company_name"):
        owner = sh["external_company_name"]
    elif sh.get("person_name"):
        owner = sh["person_name"]
        otype = "person"
    if not owner:
        return None, None, False
    existed = session.execute(
        select(Entity.id).where(Entity.entity_type == otype, Entity.name == owner)
    ).scalar_one_or_none() is not None
    ent = writer.upsert_entity(session, otype, owner, source="external-api")
    return otype, ent.id, (not existed)


def _normalize_status(rec: dict) -> str:
    """公司状态：文档显式 status（opened/closed）优先；否则 closing_date 有→closed，否则按 is_active。"""
    s = (rec.get("status") or "").strip().lower()
    if s in ("opened", "closed"):
        return s
    if rec.get("closing_date"):
        return "closed"
    return "opened" if rec.get("is_active", True) else "closed"


def import_external_companies(session: Session, companies: list[dict]) -> dict:
    """公司列表（外部 API 原始 dict）→ 幂等 upsert company 实体 + 股权关系。

    可离线直接喂 dict 单测，不联网。返回统计数据。"""
    stats = {"companies": 0, "companies_created": 0,
             "rels": 0, "persons_created": 0}
    for rec in companies:
        name = (rec.get("name") or "").strip()
        if not name:
            continue
        fields = {
            "external_id": rec.get("id"),
            "opening_date": rec.get("opening_date"),
            "closing_date": rec.get("closing_date"),
            "is_active": rec.get("is_active"),
        }
        comp = writer.upsert_entity(
            session, "company", name, source="external-api", fields=fields)
        # created 判定：status 原来为空而本次落到非空（只增不减；新写才置 status）
        if comp.status is None:
            stats["companies_created"] += 1
        comp.status = _normalize_status(rec)

        # 股权结构 → 股东 owner 实体 + holds 边 + 持股比归集
        pct_map: dict[str, float] = {}
        opening_d = _year(rec.get("opening_date"))
        closing_d = _year(rec.get("closing_date"))
        for sh in rec.get("shareholders") or []:
            otype, oid, created = _shareholder_owner(session, sh)
            if oid is None:
                continue
            if created and otype == "person":
                stats["persons_created"] += 1
            rel = writer.upsert_relationship(
                session, oid, comp.id, "holds",
                since_year=opening_d, until_year=closing_d,
            )
            if rel is not None:
                stats["rels"] += 1
            owner_name = sh.get("internal_company_name") or sh.get("external_company_name") \
                or sh.get("person_name")
            pct = sh.get("ownership_pct")
            if owner_name and pct is not None:
                pct_map[owner_name] = float(pct)
        if pct_map:
            comp.fields = {**(comp.fields or {}), "shareholders_pct": pct_map}

        stats["companies"] += 1
    return stats


def run_external_company_import(session: Session, *,
                                base_url: str | None = None,
                                client: httpx.Client | None = None) -> dict:
    """端到端：load 凭据 → login → fetch 公司 → import。返回 import 统计。"""
    url, username, password = load(base_url=base_url)
    token = login(url, username, password, client=client)
    companies = fetch_companies(url, token, client=client)
    return import_external_companies(session, companies)