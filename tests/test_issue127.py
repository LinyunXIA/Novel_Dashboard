"""issue #127 回归：REST 契约细节。

- timeline-events：page=0 → 422、?as_of 过滤、PUT 全量替换；
- 201 响应带 Location（§14.1）；
- entities ?status= 过滤；
- notifications PATCH 接受 {"read_at":"now"}，非法值 422。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, Integer, create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.app import app
from app.api.deps import get_db
from app.db import Base
from app.model import Entity, Notification, ReturnCurve, TimelineEvent


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

    def _seed():
        s = Session()
        s.add_all([
            TimelineEvent(event_year=1990, title="事件A", event_date=date(1990, 6, 1)),
            TimelineEvent(event_year=2005, title="事件B"),
            Entity(entity_type="company", name="CoA", status="opened"),
            Entity(entity_type="company", name="CoB", status="closed"),
            Notification(kind="recompute-done", title="t", message="m"),
        ])
        s.commit()
        s.close()

    _seed()

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


from datetime import date  # noqa: E402


def test_timeline_page_zero_is_422(client):
    c, _ = client
    assert c.get("/api/v1/timeline-events?page=0").status_code == 422


def test_timeline_page_size_over_cap_422(client):
    c, _ = client
    assert c.get("/api/v1/timeline-events?page_size=501").status_code == 422


def test_timeline_as_of_filters_future_year_only_events(client):
    c, _ = client
    r1995 = {e["title"] for e in c.get("/api/v1/timeline-events?as_of=1995-12-31").json()["items"]}
    assert "事件A" in r1995 and "事件B" not in r1995
    rall = {e["title"] for e in c.get("/api/v1/timeline-events").json()["items"]}
    assert {"事件A", "事件B"} <= rall


def test_timeline_put_full_replace_clears_note(client):
    c, _ = client
    # PUT 仅作用用户覆盖行：先建一条
    created = c.post("/api/v1/timeline-events",
                     json={"event_year": 1990, "title": "待替换", "note": "旧备注"})
    assert created.status_code == 201
    eid = created.json()["timeline_event_id"]
    r = c.put(f"/api/v1/timeline-events/{eid}",
              json={"event_year": 1991, "title": "事件A改"})
    assert r.status_code == 200
    row = [e for e in c.get("/api/v1/timeline-events").json()["items"]
           if e["id"] == eid][0]
    assert row["event_year"] == 1991 and row["note"] is None


def test_location_header_on_entity_create(client):
    c, _ = client
    r = c.post("/api/v1/entities", json={"entity_type": "person", "name": "L1"},
               headers={"X-Importer": "1"})
    assert r.status_code == 201
    assert r.headers.get("location") == f"/api/v1/entities/{r.json()['id']}"


def test_entities_status_filter(client):
    c, _ = client
    names = {e["name"] for e in
             c.get("/api/v1/entities?type=company&status=closed").json()["items"]}
    assert names == {"CoB"}


def test_notifications_patch_body_validation(client):
    c, Session = client
    s = Session()
    nid = s.execute(select(Notification.id)).scalars().first()
    s.close()
    bad = c.patch(f"/api/v1/notifications/{nid}", json={"read_at": "yesterday"})
    assert bad.status_code == 422
    ok = c.patch(f"/api/v1/notifications/{nid}", json={"read_at": "now"})
    assert ok.status_code == 200 and ok.json()["read"] is True
