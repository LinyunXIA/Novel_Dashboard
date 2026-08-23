"""Unit tests for character 关系持久化（issue #27 回归）。

覆盖：
- parse_character：姓名不再重复进 rels（只「与主角的关系」/「关系」进 rels）
- import_characters：rels → 按 target 名查 entity → upsert Relationship
- 失配名（target 未注册）→ warnings（不阻塞）
- 自环（from==to）跳过
- 幂等：同 (from,to,rel_type) 二次 import 不重复
- parse_relationship 死代码已删（import 失败）
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.ingest.parsers import parse_character
from app.ingest.writer import import_characters, upsert_relationship
from app.model import Entity, Relationship


@pytest.fixture
def session():
    from sqlalchemy import BigInteger, Integer
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def _write(tmp: Path, name: str, content: str) -> Path:
    p = tmp / name
    p.write_text(content, encoding="utf-8")
    return p


class TestParseCharacterCleanRels:
    def test_name_not_in_rels(self, tmp_path):
        """issue #27 修复：「姓名」仅决定 name，不重复进 rels。"""
        p = _write(tmp_path, "养父.md", (
            "- 姓名：Joren Peeters\n"
            "- 与主角的关系：养父\n"
            "- 职业：律师\n"
        ))
        recs = parse_character(p)
        assert recs[0]["name"] == "Joren Peeters"
        # rels 只含关系，不含姓名
        rel_keys = [k for k, _ in recs[0]["relations"]]
        assert "姓名" not in rel_keys
        assert "与主角的关系" in rel_keys

    def test_relations_only_rel_fields(self, tmp_path):
        p = _write(tmp_path, "主角.md", (
            "- 姓名：Stijn\n"
            "- 与主角的关系：本人\n"
            "- 关系：被收养于 Henri 家族\n"
            "- 出生：1985\n"
        ))
        recs = parse_character(p)
        # rels 含两个关系键，其他进 fields
        rel_keys = {k for k, _ in recs[0]["relations"]}
        assert rel_keys == {"与主角的关系", "关系"}
        assert "出生" in recs[0]["fields"]


class TestImportCharactersRelationships:
    def _seed_related_entities(self, session):
        """预注册 target 实体，让关系可命中。"""
        session.add_all([
            Entity(entity_type="person", name="Stijn"),
            Entity(entity_type="person", name="Joren Peeters"),
            Entity(entity_type="person", name="Henri Peeters"),
        ])
        session.commit()

    def test_rels_persisted(self, session, tmp_path):
        """issue #27 核心修复：rels 持久化进 relationship 表。

        val 作为 target entity 名字查 entity；命中 → 写 Relationship。
        （真实文件 val 常为关系描述如「养父」而非名字——按 issue #27 字面要求 val=名字）
        """
        self._seed_related_entities(session)
        p = _write(tmp_path, "养父.md", (
            "- 姓名：Joren Peeters\n"
            "- 关系：Stijn\n"  # val=Stijn（entity.name），rel_type="关系"
        ))
        recs = parse_character(p)
        stats = import_characters(session, recs, source_file="养父.md")
        assert stats["rels"] == 1
        assert stats["warnings"] == []
        # 验证 relationship 表
        from_entity = session.query(Entity).filter_by(name="Joren Peeters").first()
        to_entity = session.query(Entity).filter_by(name="Stijn").first()
        rel = session.query(Relationship).filter_by(
            from_entity_id=from_entity.id, to_entity_id=to_entity.id,
            rel_type="关系"
        ).first()
        assert rel is not None
        assert rel.source_file == "养父.md"

    def test_missing_target_warns(self, session, tmp_path):
        """target 未注册 → warning（不阻塞，DESIGN §6.3 失配可人工补 entity）。"""
        # 只注册 Stijn，Joren 未注册
        session.add(Entity(entity_type="person", name="Stijn"))
        session.commit()
        p = _write(tmp_path, "养父.md", (
            "- 姓名：Joren Peeters\n"
            "- 关系：Stijn\n"
        ))
        recs = parse_character(p)
        stats = import_characters(session, recs)
        # Joren 第一次注册（首次见）
        assert stats["imported"] == 1
        assert stats["rels"] == 1
        # 第二次引用一个不存在的 entity → warning
        p2 = _write(tmp_path, "管家.md", (
            "- 姓名：管家 A\n"
            "- 关系：某某不存在\n"
        ))
        recs2 = parse_character(p2)
        stats2 = import_characters(session, recs2)
        assert stats2["rels"] == 0
        assert len(stats2["warnings"]) == 1
        assert "未注册" in stats2["warnings"][0]

    def test_self_loop_skipped(self, session, tmp_path):
        """rels 的 target 等于 name → 自环跳过（避免 entity 自指）。"""
        session.add(Entity(entity_type="person", name="Stijn"))
        session.commit()
        p = _write(tmp_path, "主角.md", (
            "- 姓名：Stijn\n"
            "- 与主角的关系：本人\n"
        ))
        recs = parse_character(p)
        stats = import_characters(session, recs)
        assert stats["rels"] == 0
        assert session.query(Relationship).count() == 0

    def test_idempotent(self, session, tmp_path):
        """二次 import 同 (from,to,rel_type) 不重复。"""
        self._seed_related_entities(session)
        p = _write(tmp_path, "养父.md", (
            "- 姓名：Joren Peeters\n"
            "- 关系：Stijn\n"
        ))
        recs = parse_character(p)
        import_characters(session, recs)
        first_count = session.query(Relationship).count()
        import_characters(session, recs)
        second_count = session.query(Relationship).count()
        assert first_count == second_count == 1


class TestParseRelationshipRemoved:
    def test_dead_code_removed(self):
        """issue #27：parse_relationship 死代码已删；import 应失败。"""
        with pytest.raises(ImportError):
            from app.ingest.normalize import parse_relationship  # noqa: F401


class TestUpsertRelationship:
    def test_returns_none_on_self_loop(self, session):
        a = Entity(entity_type="person", name="X")
        session.add(a)
        session.flush()
        assert upsert_relationship(session, a.id, a.id, "self") is None

    def test_creates_then_returns_existing(self, session):
        a = Entity(entity_type="person", name="A")
        b = Entity(entity_type="person", name="B")
        session.add_all([a, b])
        session.flush()
        r1 = upsert_relationship(session, a.id, b.id, "parent")
        session.commit()
        r2 = upsert_relationship(session, a.id, b.id, "parent")
        assert r1 is not None and r1.id == r2.id
        assert session.query(Relationship).count() == 1