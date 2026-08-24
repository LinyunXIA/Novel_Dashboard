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
from app.model import Account, Entity, ExchangeRate, ReturnCurve, Snapshot, TimelineEvent


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


class TestCorsMiddleware:
    """issue #30：CORSMiddleware 已挂载，PRD §13 限本地两个来源。"""

    def test_cors_allows_localhost_5173(self, client):
        """issue #30 修复点：vite dev 端口跨域通过。"""
        c, _ = client
        r = c.get("/api/v1/ping", headers={"Origin": "http://localhost:5173"})
        assert r.status_code == 200
        # CORS 预检响应头
        assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"

    def test_cors_allows_127_5173(self, client):
        c, _ = client
        r = c.get("/api/v1/ping", headers={"Origin": "http://127.0.0.1:5173"})
        assert r.headers.get("access-control-allow-origin") == "http://127.0.0.1:5173"

    def test_cors_rejects_other_origin(self, client):
        """非白名单来源 → 不回 ACAO 头（浏览器会拦截）。"""
        c, _ = client
        r = c.get("/api/v1/ping", headers={"Origin": "http://evil.example.com"})
        assert r.status_code == 200  # 实际请求仍 OK，但 CORS 头不返回
        assert r.headers.get("access-control-allow-origin") is None

    def test_cors_middleware_registered(self):
        """CORSMiddleware 在 app.user_middleware 中（issue #30 修复点）。"""
        from app.api.app import _CORS_ORIGINS
        assert "http://localhost:5173" in _CORS_ORIGINS
        assert "http://127.0.0.1:5173" in _CORS_ORIGINS
        cors_mw = [m for m in app.user_middleware if "CORS" in m.cls.__name__]
        assert len(cors_mw) == 1


class TestIssue87Endpoints:
    """issue #87：/returns/countries 动态国家列表 + exchange-rates 按年筛方向。

    两个新端点是"静态路由先于动态 /returns、/exchange-rates"展开的补充；
    验证不吞路由且按预期返回。
    """

    def test_returns_countries_distinct(self, client):
        c, Session = client
        s = Session()
        s.add(ReturnCurve(country="比利时", risk_lvl="R3", year=1980, rate=10.0))
        s.add(ReturnCurve(country="比利时", risk_lvl="R2", year=1981, rate=5.0))
        s.add(ReturnCurve(country="卢森堡", risk_lvl="R3", year=1980, rate=8.0))
        s.commit()
        r = c.get("/api/v1/returns/countries")
        assert r.status_code == 200
        assert r.json()["countries"] == ["卢森堡", "比利时"]      # distinct + 排序（字典序）

    def test_exchange_rates_year_filter_and_routes_static_first(self, client):
        c, Session = client
        s = Session()
        s.add(ExchangeRate(fx_from="BEF", fx_to="EUR", year=1980, rate=0.024789))
        s.add(ExchangeRate(fx_from="BEF", fx_to="EUR", year=1981, rate=0.025))
        s.add(ExchangeRate(fx_from="BEF", fx_to="USD", year=1980, rate=0.02))
        s.commit()
        # 静态路由 /exchange-rates 与 /returns 不受 /returns/countries 影响
        r = c.get("/api/v1/exchange-rates?year=1980&page_size=50")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 2                               # 仅 1980 两条（1981 被 year 筛掉）
        # /exchange-rates?year=1 的长整形校验（year 非路由后缀）
        r2 = c.get("/api/v1/exchange-rates?year=1981")
        assert [i["from"] for i in r2.json()["items"]] == ["BEF"]