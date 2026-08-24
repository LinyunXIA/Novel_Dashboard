"""人物 / 公司图谱只读视图（DESIGN §14 graph · F-P1-04/05 只读部分）。

从既有 entity + relationship 聚合出节点边，供前端力导向 SVG 渲染。
只读：本轮不建 importers / 外部 API①② 对接（用户单独提供文档后处理）。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.model import Entity, Relationship


def _graph(session: Session, entity_type: str) -> dict:
    """按 entity_type 聚合 nodes + edges（两端都在该类型集合内的关系）。

    仅返回纯类型视图（人—人 或 公司—公司）；跨类型边（人—公司）见 all_graph（issue #84）。
    """
    ents = {e.id: e for e in session.execute(
        select(Entity).where(Entity.entity_type == entity_type)).scalars().all()}
    nodes = [
        {"id": e.id, "type": e.entity_type, "name": e.display_name or e.name,
         "display_name": e.display_name, "status": e.status}
        for e in ents.values()
    ]
    edges = []
    for r in session.execute(select(Relationship)).scalars().all():
        f, t = ents.get(r.from_entity_id), ents.get(r.to_entity_id)
        if f is None or t is None:
            continue
        edges.append({
            "from": f.id, "to": t.id,
            "from_name": f.display_name or f.name,
            "to_name": t.display_name or t.name,
            "rel_type": r.rel_type,
            "since_year": r.since_year, "until_year": r.until_year,
        })
    return {"nodes": nodes, "edges": edges, "node_count": len(nodes), "edge_count": len(edges)}


def person_graph(session: Session) -> dict:
    """人—人关系图（纯类型视图；跨类型边见 /graph/all）。"""
    return _graph(session, "person")


def company_graph(session: Session) -> dict:
    """公司—公司关系图（纯类型视图；跨类型边见 /graph/all）。"""
    return _graph(session, "company")


def all_graph(session: Session) -> dict:
    """全量图谱（issue #84 · PRD §6.4 P1-1）：节点=所有实体（含 person/company/asset/family），
    边=所有 relationship（含人—公司/资产等跨类型）。

    现有纯类型端点 /graph/persons、/graph/companies 保留不删；本视图作为跨类型视图供
    「人—人、人—公司」合并关系可视化（前端按 entity_type 着色区分）。
    """
    ents = {e.id: e for e in session.execute(select(Entity)).scalars().all()}
    nodes = [
        {"id": e.id, "type": e.entity_type, "name": e.display_name or e.name,
         "display_name": e.display_name, "status": e.status}
        for e in ents.values()
    ]
    edges = []
    for r in session.execute(select(Relationship)).scalars().all():
        f, t = ents.get(r.from_entity_id), ents.get(r.to_entity_id)
        if f is None or t is None:
            continue
        edges.append({
            "from": f.id, "to": t.id,
            "from_type": f.entity_type, "to_type": t.entity_type,
            "from_name": f.display_name or f.name,
            "to_name": t.display_name or t.name,
            "rel_type": r.rel_type,
            "since_year": r.since_year, "until_year": r.until_year,
        })
    return {"nodes": nodes, "edges": edges, "node_count": len(nodes), "edge_count": len(edges)}