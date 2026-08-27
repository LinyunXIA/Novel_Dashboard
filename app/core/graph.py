"""人物 / 公司图谱只读视图（DESIGN §14 graph · F-P1-04/05 只读部分）。

从既有 entity + relationship 聚合出节点边，供前端力导向 SVG 渲染。
#197：在显式 `relationship` 行（实线）之外，并入 `app.core.kinship` 亲缘**推理边**（虚线），
并把 `source_file='infer-suppressed'` 的抑制标记（UI 删除推理边时写入）用于隐藏对应推理边
（删除不复活）。显式行排除抑制标记。
"""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.model import Entity, Relationship
from app.core.kinship import infer_person_edges

SUPPRESS_SRC = "infer-suppressed"


def _merged_edges(session: Session, ents: dict[int, Entity]) -> list[dict]:
    """显式 relationship 行（实线）+ 亲缘推理边（虚线，已去被抑制项），两端都须在 ents 内。"""
    out: list[dict] = []
    supp: set[tuple[int, int, str]] = set()
    for r in session.execute(
            select(Relationship).where(Relationship.source_file == SUPPRESS_SRC)).scalars().all():
        supp.add((r.from_entity_id, r.to_entity_id, r.rel_type))
    explicit_keys: set[tuple[int, int, str]] = set()
    for r in session.execute(
            select(Relationship).where(
                or_(Relationship.source_file != SUPPRESS_SRC,
                    Relationship.source_file.is_(None)))).scalars().all():
        f, t = ents.get(r.from_entity_id), ents.get(r.to_entity_id)
        if f is None or t is None:
            continue
        explicit_keys.add((r.from_entity_id, r.to_entity_id, r.rel_type))
        out.append({
            "id": r.id, "from": f.id, "to": t.id,
            "from_type": f.entity_type, "to_type": t.entity_type,
            "from_name": f.display_name or f.name,
            "to_name": t.display_name or t.name,
            "rel_type": r.rel_type,
            "since_year": r.since_year, "until_year": r.until_year,
            "inferred": False,
        })
    for e in infer_person_edges(session):
        f, t = ents.get(e["from"]), ents.get(e["to"])
        if f is None or t is None:
            continue
        if (e["from"], e["to"], e["rel_type"]) in supp:      # 已被 UI 抑制，不复活
            continue
        if (e["from"], e["to"], e["rel_type"]) in explicit_keys:  # 已有人工实线，不重复虚线
            continue
        out.append({
            "id": None, "from": f.id, "to": t.id,
            "from_type": f.entity_type, "to_type": t.entity_type,
            "from_name": f.display_name or f.name,
            "to_name": t.display_name or t.name,
            "rel_type": e["rel_type"], "note": e.get("note"),
            "inferred": True,
        })
    return out


def _graph(session: Session, entity_type: str) -> dict:
    """按 entity_type 聚合 nodes + edges（两端都在该类型集合内的关系）。"""
    ents = {e.id: e for e in session.execute(
        select(Entity).where(Entity.entity_type == entity_type)).scalars().all()}
    nodes = [
        {"id": e.id, "type": e.entity_type, "name": e.display_name or e.name,
         "display_name": e.display_name, "status": e.status}
        for e in ents.values()
    ]
    edges = _merged_edges(session, ents)
    return {"nodes": nodes, "edges": edges, "node_count": len(nodes), "edge_count": len(edges)}


def person_graph(session: Session) -> dict:
    """人—人关系图（含亲缘推理边；跨类型边见 /graph/all）。"""
    return _graph(session, "person")


def company_graph(session: Session) -> dict:
    """公司—公司关系图（显式边；推理边无公司两端自然不出现）。"""
    return _graph(session, "company")


def all_graph(session: Session) -> dict:
    """全量图谱（issue #84 · PRD §6.4 P1-1）：节点=所有实体，边=显式 + 亲缘推理。"""
    ents = {e.id: e for e in session.execute(select(Entity)).scalars().all()}
    nodes = [
        {"id": e.id, "type": e.entity_type, "name": e.display_name or e.name,
         "display_name": e.display_name, "status": e.status}
        for e in ents.values()
    ]
    edges = _merged_edges(session, ents)
    return {"nodes": nodes, "edges": edges, "node_count": len(nodes), "edge_count": len(edges)}