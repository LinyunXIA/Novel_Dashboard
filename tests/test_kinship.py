"""#197 亲缘推理 + 图谱之抑制测试（app/core/kinship.py · graph.py）。

- 6 人真实称谓 → 推理边（祖先/夫妻）；补养父养母后 → 父子/母女/夫妻。
- person_graph 并入推理边(inferred)；`infer-suppressed` 标记隐藏对应推理边（不复活）。
内存 SQLite（BigInteger 主键降级，同 tests/test_api.py 模式）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, Integer, create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.kinship import infer_person_edges
from app.core.graph import SUPPRESS_SRC, person_graph
from app.model import Base, Entity, Relationship


@pytest.fixture()
def session():
    for t in Base.metadata.tables.values():
        for c in t.columns:
            if isinstance(c.type, BigInteger) and c.primary_key:
                c.type = Integer()
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    s = sessionmaker(bind=e)()
    yield s
    s.close()
    e.dispose()


def _id(session, name):
    return session.execute(select(Entity.id).where(Entity.name == name)).scalar()


def _seed_core(session):
    for name, role in [
        ("Stijn Peeters", None),
        ("Henri Peeters", "养父的父亲"),
        ("养祖母", "养父的母亲"),
        ("Frederik van Oranje", "养母的父亲"),
        ("养外祖母", "养母的母亲"),
        ("先祖", "家族始祖，主角Stijn的十五世祖"),
    ]:
        session.add(Entity(entity_type="person", name=name,
                           fields={} if role is None else {"与主角的关系": role}))
    session.commit()


def _es(edges):
    return {(e["from"], e["to"], e["rel_type"]) for e in edges}


def test_infer_core_roles(session):
    _seed_core(session)
    h, g = _id(session, "Henri Peeters"), _id(session, "养祖母")
    f, w = _id(session, "Frederik van Oranje"), _id(session, "养外祖母")
    a, s = _id(session, "先祖"), _id(session, "Stijn Peeters")
    es = _es(infer_person_edges(session))
    assert (a, s, "祖先") in es
    assert (h, g, "夫妻") in es
    assert (f, w, "夫妻") in es
    assert len(es) == 3                       # 养父/养母未导入，无父子边


def test_infer_with_parents(session):
    _seed_core(session)
    session.add(Entity(entity_type="person", name="Joren Peeters", fields={"与主角的关系": "养父"}))
    session.add(Entity(entity_type="person", name="Johanna Peeters", fields={"与主角的关系": "养母"}))
    session.commit()
    h = _id(session, "Henri Peeters")
    j = _id(session, "Joren Peeters")
    jo = _id(session, "Johanna Peeters")
    s = _id(session, "Stijn Peeters")
    es = _es(infer_person_edges(session))
    assert (h, j, "父子") in es               # 祖父 → 养父
    assert (j, s, "父子") in es               # 养父 → 主角
    assert (jo, s, "母女") in es              # 养母 → 主角
    assert (j, jo, "夫妻") in es


def test_person_graph_merge_and_suppress(session):
    _seed_core(session)
    g1 = person_graph(session)
    inf1 = [e for e in g1["edges"] if e["inferred"]]
    assert len(inf1) == 3 and g1["edge_count"] == 3
    # 抑制 Henri↔养祖母（夫妻）→ 隐藏且不复活
    h, gm = _id(session, "Henri Peeters"), _id(session, "养祖母")
    session.add(Relationship(from_entity_id=h, to_entity_id=gm,
                             rel_type="夫妻", source_file=SUPPRESS_SRC))
    session.commit()
    g2 = person_graph(session)
    assert len([e for e in g2["edges"] if e["inferred"]]) == 2
    assert not any(e["from"] == h and e["to"] == gm and e["rel_type"] == "夫妻"
                   for e in g2["edges"])
    # 显式实线同键推理不重复：+ 一条人工(Frederik↔养外祖母=夫妻) → 该推理虚线不再出现
    f, w = _id(session, "Frederik van Oranje"), _id(session, "养外祖母")
    session.add(Relationship(from_entity_id=f, to_entity_id=w, rel_type="夫妻", source_file="ui"))
    session.commit()
    g3 = person_graph(session)
    solid = [e for e in g3["edges"] if not e["inferred"]]
    assert any(e["from"] == f and e["to"] == w and e["rel_type"] == "夫妻" for e in solid)
    assert all(e["id"] is not None for e in solid)