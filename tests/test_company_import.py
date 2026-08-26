"""公司图谱外部 API① 导入（F-P1-05 · DESIGN §13）单测。

覆盖：
- import_external_companies：doc 样例映射（company 实体 + 自然人股东建 person 节点 +
  rel_type='holds' 边 + status 映射 + fields 记开停业/持股比）。
- 只增不减 / 幂等：同一批数据跑两次 → entity/关系计数不变，已存在公司不被新建。
- 端点 POST /graph/companies/import：monkeypatch run_external_company_import 注入数据 →
  200 + stats + 刷新后 graph（免网）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.app import app
from app.api.deps import get_db
from app.db import Base
from app.ingest import writer
from app.ingest.importers import company_info
from app.model import Entity, Relationship


@pytest.fixture
def session():
    """内存 SQLite 会话（StaticPool 共享连接；BigInteger 主键降级 Integer）。"""
    from sqlalchemy import BigInteger, Integer

    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    s = S()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _sample_companies():
    return [
        {
            "id": 5,
            "name": "Peeters Luxembourg S.à r.l.",
            "is_active": True,
            "opening_date": "1990-01-01",
            "closing_date": None,
            "status": "opened",
            "shareholders": [
                {"internal_company_name": "Family Asset Management SPRL",
                 "ownership_pct": 100.0},
                {"person_name": "Henri Peeters", "ownership_pct": 0.0},
            ],
        },
        {
            "id": 9,
            "name": "Peeters Asia Ltd",
            "is_active": False,
            "opening_date": "1995-06-01",
            "closing_date": "2005-12-31",
            "status": "closed",
            "shareholders": [
                {"external_company_name": "Far East Holdings", "ownership_pct": 60.0},
            ],
        },
    ]


def _entities(s):
    return {e.name: e for e in s.execute(select(Entity)).scalars().all()}


class TestImportMapping:
    def test_company_person_and_external_shareholders_map(self, session):
        stats = company_info.import_external_companies(session, _sample_companies())
        session.commit()

        assert stats["companies"] == 2
        assert stats["companies_created"] == 2
        # 两个 company 实体 + 自然人股东 Henri Peeters
        ent = _entities(session)
        assert ent["Peeters Luxembourg S.à r.l."].entity_type == "company"
        assert ent["Peeters Luxembourg S.à r.l."].status == "opened"
        assert ent["Peeters Asia Ltd"].status == "closed"
        assert ent["Family Asset Management SPRL"].entity_type == "company"
        assert ent["Far East Holdings"].entity_type == "company"
        assert ent["Henri Peeters"].entity_type == "person"
        assert "opening_date" in ent["Peeters Luxembourg S.à r.l."].fields

        # holds 边：Family AM → Lux，Henri → Lux，Far East → Asia（rel_type='holds' + 年窗）
        rels = session.execute(select(Relationship)).scalars().all()
        assert len(rels) == 3
        assert set(r.rel_type for r in rels) == {"holds"}
        # 直接校验年窗落点
        lux = ent["Peeters Luxembourg S.à r.l."]
        lux_rels = [r for r in rels if r.to_entity_id == lux.id]
        assert all(r.since_year == 1990 for r in lux_rels)
        asia = ent["Peeters Asia Ltd"]
        asia_rel = [r for r in rels if r.to_entity_id == asia.id][0]
        assert asia_rel.since_year == 1995 and asia_rel.until_year == 2005

        # 持股比落 fields.shareholders_pct
        assert ent["Peeters Asia Ltd"].fields["shareholders_pct"]["Far East Holdings"] == 60.0

    def test_idempotent_reimport_never_grows(self, session):
        company_info.import_external_companies(session, _sample_companies())
        session.commit()
        before = _entities(session)
        n_ent = len(before)
        n_rel = len(session.execute(select(Relationship)).scalars().all())

        stats = company_info.import_external_companies(session, _sample_companies())
        session.commit()

        assert stats["companies_created"] == 0  # 只增不减：已存在不新建
        assert len(_entities(session)) == n_ent
        assert len(session.execute(select(Relationship)).scalars().all()) == n_rel

    def test_status_derived_when_absent(self, session):
        data = [
            {"name": "A", "closing_date": "2000-01-01", "is_active": False},
            {"name": "B", "is_active": True},
        ]
        company_info.import_external_companies(session, data)
        session.commit()
        ent = _entities(session)
        assert ent["A"].status == "closed"
        assert ent["B"].status == "opened"


class TestImportEndpoint:
    def test_post_import_returns_stats_and_refreshed_graph(self, session):
        def _fake_run(db, **_kw):
            # 注入与真实 importer 相同的写库逻辑（不联网）
            return company_info.import_external_companies(db, _sample_companies())

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(company_info, "run_external_company_import", _fake_run)
        try:
            # 端点依赖注入
            app.dependency_overrides[get_db] = lambda: session
            with TestClient(app) as c:
                r = c.post("/api/v1/graph/companies/import")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["stats"]["companies"] == 2
            g = body["graph"]  # 纯公司图谱视图：只含 company 节点/边
            names = {n["name"] for n in g["nodes"]}
            assert {"Peeters Luxembourg S.à r.l.", "Peeters Asia Ltd",
                    "Family Asset Management SPRL", "Far East Holdings"} <= names
            assert "Henri Peeters" not in names  # person 节点不进 company 视图（仍在 DB）
            assert g["edge_count"] >= 2  # Family AM→Lux、Far East→Asia 两条 company 间 holds
            db_names = {e.name for e in session.execute(select(Entity)).scalars().all()}
            assert "Henri Peeters" in db_names
        finally:
            monkeypatch.undo()
            app.dependency_overrides.pop(get_db, None)


class TestApiRoot:
    def test_api_root_adds_and_dedupes_prefix(self):
        assert company_info._api_root("http://127.0.0.1:7273") == "http://127.0.0.1:7273/api/v1"
        assert company_info._api_root("http://127.0.0.1:7273/") == "http://127.0.0.1:7273/api/v1"
        assert company_info._api_root("http://127.0.0.1:7273/api/v1") == "http://127.0.0.1:7273/api/v1"
        assert company_info._api_root("http://127.0.0.1:8000/api/v1") == "http://127.0.0.1:8000/api/v1"


class TestWriterExtras:
    def test_upsert_relationship_sets_until_year_on_existing(self, session):
        a = writer.upsert_entity(session, "company", "A")
        b = writer.upsert_entity(session, "company", "B")
        writer.upsert_relationship(session, a.id, b.id, "holds", since_year=1990)
        rel = writer.upsert_relationship(session, a.id, b.id, "holds", until_year=2000)
        session.commit()
        assert rel.since_year == 1990
        assert rel.until_year == 2000  # 同键复用并更新年窗

# ---- 外部 API v1.6/v2.6 更新适配（tax_zone 字段落 fields）----
def test_tax_zone_fields_persisted(session):
    """v1.6/v2.6 R1：tax_zone_id/tax_zone_label 随公司信息落 entity.fields 备查。"""
    recs = [{"id": 77, "name": "税区对照公司", "is_active": True,
             "tax_zone_id": 3, "tax_zone_label": "比利时（国家级）",
             "shareholders": []}]
    company_info.import_external_companies(session, recs)
    session.flush()
    ent = session.query(Entity).filter(Entity.name == "税区对照公司").one()
    assert ent.fields["tax_zone_id"] == 3
    assert ent.fields["tax_zone_label"] == "比利时（国家级）"


def test_tax_zone_absent_is_none_not_missing_key(session):
    """旧版响应无 tax_zone 字段 → fields 显式 None（不缺键，消费方安全 .get）。"""
    recs = [{"id": 78, "name": "旧版无税区公司", "is_active": True}]
    company_info.import_external_companies(session, recs)
    session.flush()
    ent = session.query(Entity).filter(Entity.name == "旧版无税区公司").one()
    assert ent.fields.get("tax_zone_id") is None
    assert ent.fields.get("tax_zone_label") is None
