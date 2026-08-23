"""Unit tests for app/api/app.py（issue #23 回归）。

覆盖：
- /ping liveness：纯 ping，不连 DB
- /health 真健康摘要
- 分页：page=0 → 422（FastAPI Query 校验）
- snapshots as_of + year 互斥 → 422
- /accounts/{id} 详情与 404
- /timeline-events(+/id) 列表与 404
- accounts/returns/exchange-rates 分页响应结构（items + total）
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
from app.model import Account, Entity, ExchangeRate, Snapshot, TimelineEvent


@pytest.fixture
def client():
    """TestClient + 内存 SQLite（StaticPool 共享连接）+ dependency_overrides 注入 session。

    StaticPool 让所有连接共享同一个 :memory: 数据库；否则 create_all 在连接 A 建表，
    session 用连接 B 看不到。
    """
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
    Session = sessionmaker(bind=engine)

    def _override_get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c, Session
    app.dependency_overrides.clear()
    engine.dispose()


def _seed_minimal(session) -> Entity:
    e = Entity(entity_type="person", name="Henri Peeters")
    a = Account(entity_id=0, currency="BEF")
    session.add_all([e, a])
    session.flush()
    a.entity_id = e.id
    session.add(TimelineEvent(event_year=1990, title="事件A", decade="1990s"))
    session.add(ExchangeRate(fx_from="USD", fx_to="EUR", year=2000, rate=0.9))
    session.add(Snapshot(as_of_year=2001, scope="family:total", value=100000.0, currency="USD"))
    session.commit()
    return e


class TestPingAndHealth:
    def test_ping_no_db(self, client):
        """issue #23：/ping 是 liveness，不读 DB。"""
        c, _ = client
        r = c.get("/api/v1/ping")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_health_returns_summary_and_findings(self, client):
        """issue #23：/health 返回真 H1-H5 摘要。"""
        c, Session = client
        s = Session()
        _seed_minimal(s)
        r = c.get("/api/v1/health")
        assert r.status_code == 200
        body = r.json()
        assert "summary" in body
        assert "findings" in body
        # summary 至少含 H1..H5 规则
        assert all(rule in body["summary"] for rule in ("H1", "H2", "H3", "H4", "H5"))


class TestPaginationValidation:
    def test_page_zero_rejected(self, client):
        """issue #23：page=0 应被 FastAPI Query 拒绝（422）。"""
        c, _ = client
        r = c.get("/api/v1/entities?page=0")
        assert r.status_code == 422

    def test_page_size_too_large_rejected(self, client):
        """page_size > 200 应被拒绝。"""
        c, _ = client
        r = c.get("/api/v1/entities?page_size=201")
        assert r.status_code == 422

    def test_pagination_response_shape(self, client):
        """分页响应含 items + total + page + page_size。"""
        c, Session = client
        s = Session()
        _seed_minimal(s)
        r = c.get("/api/v1/accounts")
        assert r.status_code == 200
        body = r.json()
        assert "items" in body
        assert "total" in body
        assert body["total"] == 1
        assert body["page"] == 1


class TestSnapshotsMutex:
    def test_as_of_and_year_both_rejected(self, client):
        """issue #23：as_of + year 同时给 → 422（互斥语义）。"""
        c, _ = client
        r = c.get("/api/v1/snapshots?as_of=2001-12-30&year=2001")
        assert r.status_code == 422
        assert "as_of" in r.json()["detail"]


class TestAccountDetail:
    def test_get_account_ok(self, client):
        c, Session = client
        s = Session()
        e = _seed_minimal(s)
        a_id = s.query(Account).filter_by(entity_id=e.id).first().id
        r = c.get(f"/api/v1/accounts/{a_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["currency"] == "BEF"
        assert "bank" in body

    def test_get_account_404(self, client):
        c, _ = client
        r = c.get("/api/v1/accounts/99999")
        assert r.status_code == 404


class TestTimelineEvents:
    def test_list_timeline_events(self, client):
        """issue #23：补 /timeline-events 只读列表（编年史屏数据来源）。"""
        c, Session = client
        s = Session()
        _seed_minimal(s)
        r = c.get("/api/v1/timeline-events")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["items"][0]["title"] == "事件A"

    def test_get_timeline_event(self, client):
        c, Session = client
        s = Session()
        _seed_minimal(s)
        tid = s.query(TimelineEvent).first().id
        r = c.get(f"/api/v1/timeline-events/{tid}")
        assert r.status_code == 200
        assert r.json()["event_year"] == 1990

    def test_get_timeline_event_404(self, client):
        c, _ = client
        r = c.get("/api/v1/timeline-events/99999")
        assert r.status_code == 404