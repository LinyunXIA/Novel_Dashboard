"""Unit tests for app/core/graph.py（F-P1-04/05 只读视图）。"""
from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, Integer, create_engine
from sqlalchemy.orm import sessionmaker

from app.core.graph import all_graph, company_graph, person_graph
from app.db import Base
from app.model import Entity, Relationship


@pytest.fixture
def session():
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


def _seed(session):
    p_a = Entity(entity_type="person", name="Stijn Peeters")
    p_b = Entity(entity_type="person", name="Henri Peeters")
    p_c = Entity(entity_type="person", name="Maaike Peeters")
    c_x = Entity(entity_type="company", name="Peeters Americas")
    c_y = Entity(entity_type="company", name="Peeters Asia")
    session.add_all([p_a, p_b, p_c, c_x, c_y])
    session.flush()
    session.add_all([
        Relationship(from_entity_id=p_a.id, to_entity_id=p_b.id, rel_type="parent"),
        Relationship(from_entity_id=p_b.id, to_entity_id=p_c.id, rel_type="sibling"),
        Relationship(from_entity_id=c_x.id, to_entity_id=c_y.id, rel_type="holds"),
        Relationship(from_entity_id=p_a.id, to_entity_id=c_x.id, rel_type="member"),  # 跨界（两图都不含）
    ])
    session.flush()
    return p_a, p_b, p_c, c_x, c_y


def test_person_graph(session):
    _seed(session)
    g = person_graph(session)
    assert g["node_count"] == 3
    assert sorted(e["rel_type"] for e in g["edges"]) == ["parent", "sibling"]
    # 跨界边（person→company）应被排除出人—人图
    assert all(e["from_name"] and e["to_name"] for e in g["edges"])


def test_company_graph(session):
    _seed(session)
    g = company_graph(session)
    assert g["node_count"] == 2
    assert [e["rel_type"] for e in g["edges"]] == ["holds"]
    assert g["edges"][0]["from_name"] == "Peeters Americas"


def test_all_graph_includes_cross_edges_issue_84(session):
    """issue #84：/graph/all 包含跨类型（人—公司）边，节点 type 标注。"""
    _seed(session)
    g = all_graph(session)
    # 全量节点：3 person + 2 company = 5
    assert g["node_count"] == 5
    by_type = {}
    for n in g["nodes"]:
        by_type.setdefault(n["type"], []).append(n)
    assert len(by_type["person"]) == 3 and len(by_type["company"]) == 2
    # 跨类型边：member（person→company）应出现
    cross = [e for e in g["edges"]
             if e["from_type"] == "person" and e["to_type"] == "company"]
    assert any(e["rel_type"] == "member" for e in cross)
    # 同类型边也都在
    assert any(e["rel_type"] == "parent" for e in g["edges"])
    # 节点 type 字段填充（前端据此着色）
    assert all("type" in n and n["type"] in ("person", "company") for n in g["nodes"])