"""Unit tests for app/core/graph.py（F-P1-04/05 只读视图）。"""
from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, Integer, create_engine
from sqlalchemy.orm import sessionmaker

from app.core.graph import company_graph, person_graph
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