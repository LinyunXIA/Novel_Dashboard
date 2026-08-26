"""issue #120 回归：重算收尾三件——健康摘要进通知、最小传播起点、timeline 触发重建。"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine, Integer, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.app import app
from app.api.deps import get_db
from app.core.recompute import record_recompute_done
from app.db import Base
from app.ingest.main import _earliest_affected_year
from app.model import Notification, TimelineEvent


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


def _rec(cat: str, records):
    return SimpleNamespace(category=cat, records=records)


def test_recompute_done_notification_carries_health_summary(session):
    out = record_recompute_done(session, 1985, reason="test")
    session.commit()
    n = session.get(Notification, out["notification_id"])
    assert n.payload["start_year"] == 1985
    assert "health" in n.payload
    assert set(n.payload["health"]) >= {"H1", "H2", "H3", "H4", "H5"}


class TestEarliestAffectedYear:
    def test_global_categories_return_none(self):
        for cat in ("character", "return_table", "fx", "initial_asset", "income_security"):
            assert _earliest_affected_year(_rec(cat, [{}])) is None

    def test_timeline_min_year(self):
        r = _rec("timeline", [{"event_year": 1995}, {"event_year": 1990}, {}])
        assert _earliest_affected_year(r) == 1990

    def test_shop_y0(self):
        assert _earliest_affected_year(
            _rec("income_shop", [{"y0": 1951}, {"y0": 1947}])) == 1947

    def test_property_fixed_1974(self):
        assert _earliest_affected_year(_rec("income_property", [{}])) == 1974

    def test_bank_min_row_year(self):
        r = _rec("bank", [{"rows": [{"date": date(1993, 5, 1)}]},
                          {"rows": [{"date": date(1988, 1, 1)}]}])
        assert _earliest_affected_year(r) == 1988


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


def test_timeline_overlay_mutation_triggers_recompute_done(client):
    c, Session = client
    r = c.post("/api/v1/timeline-events",
               json={"event_year": 1992, "title": "测试事件"})
    assert r.status_code == 201
    eid = r.json()["timeline_event_id"]

    s = Session()
    notifs = s.execute(select(Notification).where(
        Notification.kind == "recompute-done")).scalars().all()
    assert len(notifs) == 1 and notifs[0].payload["start_year"] == 1992
    s.close()

    r = c.patch(f"/api/v1/timeline-events/{eid}", json={"note": "改备注"})
    assert r.status_code == 200
    s = Session()
    cnt = len(s.execute(select(Notification).where(
        Notification.kind == "recompute-done")).scalars().all())
    s.close()
    assert cnt == 2
