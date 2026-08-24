"""API-level tests for ui_ops router（F-P1-01/02/03/06 · POST /investments 等）。"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.app import app
from app.api.deps import get_db
from app.db import Base
from app.model import Account, Entity, LedgerEntry, ReturnCurve


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
    session.add(h)
    session.flush()
    a = Account(entity_id=h.id, currency="BEF")
    session.add(a)
    session.flush()
    session.add(LedgerEntry(account_id=a.id, date=date(1974, 1, 1),
                            inflow=1000, balance=1000, kind="income"))
    session.add(ReturnCurve(country="比利时", risk_lvl="R3", year=1989, rate=10.0))
    session.add(ReturnCurve(country="英国", risk_lvl="R3", year=1989, rate=12.0))
    session.commit()
    return h.id


def test_returns_regions_endpoint(client):
    c, _ = client
    r = c.get("/api/v1/returns/regions")
    assert r.status_code == 200
    body = r.json()
    assert body["欧洲"]["start_year"] == 1947
    assert body["美国"]["start_year"] == 1989
    assert body["英国"]["country"] == "英国"


def test_post_investment_201_and_recompute(client):
    c, Session = client
    eid = _seed(Session())
    r = c.post("/api/v1/investments", json={
        "year": 1989, "region": "欧洲", "risk_lvl": "R3",
        "start_date": "1989-06-30",
        "allocs": [{"entity_id": eid, "currency": "BEF", "amount": 100, "is_all": False}],
    })
    assert r.status_code == 201, r.text
    # 后传重算 → 余额已回填 + 挂了 recompute-done notification
    s = Session()
    from app.model import Notification
    assert s.query(Notification).filter(Notification.kind == "recompute-done").count() >= 1
    from app.model import LedgerEntry
    invest_row = s.query(LedgerEntry).filter(LedgerEntry.kind == "investment").first()
    assert invest_row is not None and invest_row.balance is not None


def test_post_investment_duplicate_409(client):
    c, Session = client
    eid = _seed(Session())
    body = {
        "year": 1989, "region": "欧洲", "risk_lvl": "R3", "start_date": "1989-06-30",
        "allocs": [{"entity_id": eid, "currency": "BEF", "amount": 100, "is_all": False}],
    }
    assert c.post("/api/v1/investments", json=body).status_code == 201
    r2 = c.post("/api/v1/investments", json=body)
    assert r2.status_code == 409
    assert "已投" in r2.json()["detail"]


def test_post_transfer_422_no_rate(client):
    c, Session = client
    _seed(Session())
    # 目标主体 = 另起一个 BEF 公司（同币划拨不需要汇率 → 应通过），这里测跨币缺汇率
    s = Session()
    bv = Entity(entity_type="company", name="Peeters BV")
    s.add(bv)
    s.flush()
    s.add(Account(entity_id=bv.id, currency="EUR"))
    s.commit()
    # Henri BEF → 公司 EUR 跨币，无 BEF→EUR 汇率 → 422
    src = s.query(Account).filter(Account.currency == "BEF").first().id
    r = c.post("/api/v1/transfers", json={
        "source_account_id": src, "target_entity_id": bv.id,
        "target_currency": "EUR", "amount": 10, "year": 1989,
    })
    assert r.status_code == 422
    assert "汇率" in r.json()["detail"]


def test_investment_redeemed_and_unlock_flow(client):
    c, Session = client
    eid = _seed(Session())
    body = {
        "year": 1989, "region": "欧洲", "risk_lvl": "R3", "start_date": "1989-06-30",
        "allocs": [{"entity_id": eid, "currency": "BEF", "amount": 100, "is_all": False}],
    }
    r = c.post("/api/v1/investments", json=body)
    assert r.status_code == 201
    inv_id = r.json()["id"]
    # 未赎回 → 公开 redeemed=False / locked=True
    g = c.get(f"/api/v1/investments/{inv_id}").json()
    assert g["redeemed"] is False and g["locked"] is True
    assert "redeemed" in c.get("/api/v1/investments").json()["items"][0]
    # 赎回 → redeemed=True
    assert c.post(f"/api/v1/investments/{inv_id}/redeem", json={}).status_code == 200
    assert c.get(f"/api/v1/investments/{inv_id}").json()["redeemed"] is True
    # 已赎回 → 解锁 409（issue #81）
    assert c.patch(f"/api/v1/investments/{inv_id}", json={"locked": False}).status_code == 409
    # 解锁普通投资 → 200 locked=False（另造一笔未赎回，不同地区英国）
    r2 = c.post("/api/v1/investments", json={
        **body, "region": "英国", "start_date": "1989-07-01",
    })
    assert r2.status_code == 201
    uid = r2.json()["id"]
    assert c.patch(f"/api/v1/investments/{uid}", json={"locked": False}).status_code == 200
    assert c.get(f"/api/v1/investments/{uid}").json()["locked"] is False