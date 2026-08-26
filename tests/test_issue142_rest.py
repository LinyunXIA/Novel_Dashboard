"""issue #142 回归：REST 契约细节合集。

- POST /timeline-events、POST /date-rules → 201 带 Location（#127 契约）；
- 补单条端点：GET /ledger-entries/{id}、DELETE /notifications/{id}、
  GET /source-files/{id}(+/meta 别名)、GET /entities/{id}/relationships；
- 补 §14.2 原表资源 GET /holding-events（open_only 过滤）。
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.app import app
from app.api.deps import get_db
from app.db import Base
from app.model import (Account, DateRule, Entity, HoldingEvent, LedgerEntry,
                       Notification, SourceFileVersion)


@pytest.fixture
def env():
    from sqlalchemy import BigInteger, Integer
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

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


def test_timeline_and_date_rules_201_location(env):
    c, _ = env
    r = c.post("/api/v1/timeline-events", json={
        "event_year": 1995, "title": "测试条目", "note": "n"})
    assert r.status_code == 201
    assert r.headers.get("location") == \
        f"/api/v1/timeline-events/{r.json()['timeline_event_id']}"

    r2 = c.post("/api/v1/date-rules", json={"pattern": "复活节", "resolve": "04-05"})
    assert r2.status_code == 201
    assert r2.headers.get("location") == f"/api/v1/date-rules/{r2.json()['id']}"


def test_ledger_entry_detail(env):
    c, Session = env
    s = Session()
    e = Entity(entity_type="person", name="Henri")
    s.add(e)
    s.flush()
    acc = Account(entity_id=e.id, currency="BEF", bank=None)
    s.add(acc)
    s.flush()
    le = LedgerEntry(account_id=acc.id, date=date(1990, 1, 1), reason="x",
                     inflow=10, balance=10, kind="income")
    s.add(le)
    s.commit()
    lid = le.id
    s.close()

    ok = c.get(f"/api/v1/ledger-entries/{lid}")
    assert ok.status_code == 200 and ok.json()["reason"] == "x"
    assert c.get("/api/v1/ledger-entries/99999").status_code == 404


def test_notification_delete(env):
    c, Session = env
    s = Session()
    n = Notification(kind="recompute-done", title="t", message="m")
    s.add(n)
    s.commit()
    nid = n.id
    s.close()
    assert c.delete(f"/api/v1/notifications/{nid}").status_code == 204
    assert c.delete(f"/api/v1/notifications/{nid}").status_code == 404


def test_entity_relationships_endpoint(env):
    c, Session = env
    s = Session()
    a = Entity(entity_type="person", name="A")
    b = Entity(entity_type="person", name="B")
    s.add_all([a, b])
    s.flush()
    from app.model import Relationship
    s.add(Relationship(from_entity_id=a.id, to_entity_id=b.id, rel_type="parent"))
    s.commit()
    aid, bid = a.id, b.id
    s.close()

    out = c.get(f"/api/v1/entities/{bid}/relationships").json()
    assert out["total"] == 1 and out["items"][0]["direction"] == "in"
    assert c.get("/api/v1/entities/99999/relationships").status_code == 404


def test_holding_events_listing(env):
    c, Session = env
    s = Session()
    e = Entity(entity_type="person", name="Investor")
    s.add(e)
    s.flush()
    s.add(HoldingEvent(entity_id=e.id, company="HPQ", date=date(2015, 1, 1),
                       event_type="buy", shares=100, unit_price=3.5))
    s.commit()
    s.close()

    out = c.get("/api/v1/holding-events", params={"company": "HPQ"}).json()
    assert out["total"] == 1 and out["items"][0]["event_type"] == "buy"
    opened = c.get("/api/v1/holding-events", params={"open_only": True}).json()
    assert opened["total"] == 1


def test_source_file_meta(env):
    c, Session = env
    s = Session()
    v = SourceFileVersion(file_path="基准/x.md", version=1, content="hello",
                          is_current=True)
    s.add(v)
    s.commit()
    vid = v.id
    s.close()

    for path in (f"/api/v1/source-files/{vid}", f"/api/v1/source-files/{vid}/meta"):
        r = c.get(path)
        assert r.status_code == 200
        body = r.json()
        assert body["file"] == "基准/x.md" and body["version_count"] == 1
        assert body["current_version"] == 1
