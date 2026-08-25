"""issue #112 回归：受限写通道 403 守卫全量覆盖 + create_entity 异常收窄。

- restricted.py 全部 8 个写端点：无 X-Importer header → 403（守卫生效）；
- 带 X-Importer: 1 → 守卫放行（按业务语义返回 201/404/409/422）；
- create_entity 唯一键冲突 → 409 且 detail 不泄漏 SQLAlchemy 内部错误串。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.app import app
from app.api.deps import get_db
from app.db import Base


@pytest.fixture
def client():
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
    Session = sessionmaker(bind=engine)

    def _override_get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    engine.dispose()


# (方法, 路径, body) —— 8 个受限写端点的最小合法请求体
RESTRICTED_CALLS = [
    ("post", "/api/v1/entities", {"entity_type": "person", "name": "测试人"}),
    ("put", "/api/v1/entities/999999", {"entity_type": "person", "name": "替换"}),
    ("patch", "/api/v1/entities/999999", {"status": "active"}),
    ("delete", "/api/v1/entities/999999", None),
    ("post", "/api/v1/entities/999999/relationships",
     {"to_entity_id": 1, "rel_type": "parent"}),
    ("delete", "/api/v1/relationships/999999", None),
    ("post", "/api/v1/ledger-entries", {"account_id": 1, "date": "1990-01-01", "inflow": 1}),
    ("post", "/api/v1/finance-entries",
     {"entity_id": 1, "entity_kind": "person", "year": 1990, "kind": "income", "amount": 1}),
]


@pytest.mark.parametrize("method,path,body", RESTRICTED_CALLS)
def test_restricted_endpoints_require_importer_header(client, method, path, body):
    r = client.request(method.upper(), path, json=body)
    assert r.status_code == 403, f"{method.upper()} {path} 未挂 require_importer"
    assert "importer" in r.json()["detail"]


@pytest.mark.parametrize("method,path,body,expected", [
    ("post", "/api/v1/entities", {"entity_type": "person", "name": "张三"}, 201),
    ("put", "/api/v1/entities/999999", {"entity_type": "person", "name": "替换"}, 404),
    ("patch", "/api/v1/entities/999999", {"status": "active"}, 404),
    ("delete", "/api/v1/entities/999999", None, 409),
    ("post", "/api/v1/entities/999999/relationships",
     {"to_entity_id": 1, "rel_type": "parent"}, 404),
    ("delete", "/api/v1/relationships/999999", None, 404),
    ("post", "/api/v1/ledger-entries", {"account_id": 1, "date": "bad-date"}, 422),
    ("post", "/api/v1/finance-entries",
     {"entity_id": 1, "entity_kind": "alien", "year": 1990, "kind": "income"}, 422),
])
def test_importer_header_passes_guard(client, method, path, body, expected):
    r = client.request(method.upper(), path, json=body, headers={"X-Importer": "1"})
    assert r.status_code == expected, (
        f"{method.upper()} {path} 期望 {expected} 得 {r.status_code}: {r.text[:200]}"
    )


def test_create_entity_duplicate_returns_409_without_internal_leak(client):
    h = {"X-Importer": "1"}
    body = {"entity_type": "person", "name": "重复者"}
    r1 = client.post("/api/v1/entities", json=body, headers=h)
    assert r1.status_code == 201
    r2 = client.post("/api/v1/entities", json=body, headers=h)
    assert r2.status_code == 409
    detail = r2.json()["detail"]
    assert "(entity_type=person, name=重复者)" in detail
    for leak in ("IntegrityError", "sqlite3", "SELECT", "Traceback", "sqlalchemy"):
        assert leak.lower() not in detail.lower(), f"detail 泄漏内部错误: {detail}"


def test_create_entity_invalid_type_422(client):
    r = client.post("/api/v1/entities",
                    json={"entity_type": "alien", "name": "x"},
                    headers={"X-Importer": "1"})
    assert r.status_code == 422
