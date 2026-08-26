"""Unit tests for /finance-entries API（F-P1-07 · DESIGN §14 finance-entries）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.app import app
from app.api.deps import get_db
from app.db import Base
from app.model import Entity, FinanceEntry


@pytest.fixture
def client():
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def _override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c, Session
    app.dependency_overrides.clear()
    engine.dispose()


def _seed(session):
    h = Entity(entity_type="person", name="Henri Peeters")
    bv = Entity(entity_type="company", name="Peeters BV")
    session.add_all([h, bv])
    session.flush()
    session.add_all([
        FinanceEntry(entity_id=h.id, entity_kind="person", year=1990, kind="income",
                     amount=100, currency="BEF", label="薪资", source="file"),
        FinanceEntry(entity_id=h.id, entity_kind="person", year=1991, kind="expense",
                     amount=20, currency="BEF", label="家庭支出", source="file"),
        FinanceEntry(entity_id=bv.id, entity_kind="company", year=1992, kind="income",
                     amount=500, currency="USD", label="营收", source="file"),
    ])
    session.commit()
    return h, bv


def test_list_all(client):
    c, Session = client
    _seed(Session())
    r = c.get("/api/v1/finance-entries")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert all("entity_name" in it for it in body["items"])


def test_filter_by_entity_and_kind(client):
    c, Session = client
    h, _ = _seed(Session())
    r = c.get(f"/api/v1/finance-entries?entity_id={h.id}&kind=income")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "income"
    assert items[0]["entity_name"] == "Henri Peeters"


def test_filter_by_year(client):
    c, Session = client
    _seed(Session())
    r = c.get("/api/v1/finance-entries?year=1992")
    assert r.status_code == 200   # 七轮审计 #183：显式状态码（此前隐式依赖 KeyError 失败）
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["amount"] == 500.0


def test_entity_centric(client):
    c, Session = client
    h, bv = _seed(Session())
    r = c.get(f"/api/v1/entities/{h.id}/finance-entries")
    assert r.status_code == 200
    assert r.json()["total"] == 2  # Henri 的两行

    r2 = c.get(f"/api/v1/entities/{bv.id}/finance-entries?kind=income")
    items = r2.json()["items"]
    assert len(items) == 1
    assert items[0]["entity_name"] == "Peeters BV"