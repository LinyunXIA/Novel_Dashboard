"""统一搜索（F-P1-08）单测：提取器内容 + search 检索/装配/serve 后处理 + 端点。

SQLite 无 pgvector/cos → retrieve 用 monkeypatch；LLM 用 monkeypatch 免网。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.app import app
from app.api.deps import get_db
from app.core import llm
from app.db import Base
from app.model import Entity
from app.model.labor import LaborWageBenchmark
from app.search import search as S
from app.search.extractors import EXTRACTORS


@pytest.fixture
def db():
    from sqlalchemy import BigInteger, Integer
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Sess = sessionmaker(bind=engine)
    s = Sess()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def test_clean_answer_keeps_year_and_strips_number():
    assert S.clean_answer("1947年") == "1947年"
    assert S.clean_answer("1. 祖母去世于1947年") == "祖母去世于1947年"
    assert S.clean_answer("思考：xxx\n答案：1947年") == "1947年"


def test_extractor_entity_and_wage(db):
    db.add(Entity(entity_type="person", name="Henri Peeters", display_name="亨利"))
    db.add(LaborWageBenchmark(region="比利时", year=1982, currency="BEF",
                              investment_fin_salary=1010595.0, avg_salary=673730.0))
    db.commit()
    ents = list(EXTRACTORS["entity"](db))
    assert any("亨利" in c for _, c in ents)
    wages = list(EXTRACTORS["labor_wage_benchmark"](db))
    assert wages and "1010595.0" in wages[0][1]


def test_search_logic_answer_and_hits(db, monkeypatch):
    # 注：search.py 顶层 `from app.core.llm import chat, embed` — 须 patch S.embed/S.chat
    monkeypatch.setattr(S, "embed", lambda texts, **kw: [[0.1] * 4 for _ in texts])
    monkeypatch.setattr(S, "retrieve", lambda s, q, k=8: [
        {"source_table": "timeline_event", "source_row_id": 1,
         "content": "1947 祖母去世，资产由祖父Henri继承"}])
    monkeypatch.setattr(S, "chat", lambda sys, user, **kw: "1. 祖母去世于1947年")
    r = S.search(db, "祖母哪年去世")
    assert r["hits"] and r["answer"] == "祖母去世于1947年"


def test_search_no_hits(db, monkeypatch):
    monkeypatch.setattr(S, "embed", lambda texts, **kw: [[0.0]])
    monkeypatch.setattr(S, "retrieve", lambda s, q, k=8: [])
    r = S.search(db, "无关")
    assert r["hits"] == [] and "索引中暂无" in r["answer"]


def test_search_endpoint(db, monkeypatch):
    import app.api.search as api
    result = {"answer": "1965年", "hits": [{"content": "x"}]}
    api.search = lambda d, q, as_of=None, **kw: result       # 成功
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as c:
            assert c.get("/api/v1/search", params={"q": "祖母"}).json()["answer"] == "1965年"
            assert c.get("/api/v1/search").status_code == 422   # 缺 q
            api.search = lambda d, q, as_of=None, **kw: (_ for _ in ()).throw(llm.LlmUnavailable("omlx 未启动"))
            assert c.get("/api/v1/search", params={"q": "x"}).status_code == 503
    finally:
        app.dependency_overrides.pop(get_db, None)