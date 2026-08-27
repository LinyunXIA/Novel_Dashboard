"""#197 资产聚合端点测试（GET /api/v1/entities/{id}/assets）。

验证：账户(含末余额)、初始资产、股票持仓、收益流 四组聚合，entity 名。
内存 SQLite + get_db 覆盖（同 tests/test_api.py / test_finance_api.py 模式）。
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.app import app
from app.api.deps import get_db
from app.db import Base
from app.model import Account, Entity, HoldingEvent, IncomeStream, InitialAsset, LedgerEntry


@pytest.fixture()
def client():
    for t in Base.metadata.tables.values():
        for c in t.columns:
            if isinstance(c.type, BigInteger) and c.primary_key:
                c.type = Integer()
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
    h = Entity(entity_type="person", name="Henri Peeters",
               fields={"与主角的关系": "养父的父亲"})
    session.add(h)
    session.flush()
    acc = Account(entity_id=h.id, currency="BEF", bank="Deutsche", status="active")
    session.add(acc)
    session.flush()
    session.add_all([
        LedgerEntry(account_id=acc.id, date=_dt.date(1982, 1, 1), reason="初始现金",
                    inflow=1000, outflow=None, balance=1000, kind="income"),
        LedgerEntry(account_id=acc.id, date=_dt.date(1983, 1, 1), reason="结息",
                    inflow=50, outflow=None, balance=1050, kind="income"),
        InitialAsset(entity_id=h.id, asset_type="bond", name="比利时国债",
                     currency="BEF", face_value=Decimal("82")),
        HoldingEvent(entity_id=h.id, company="GE", ticker="GE", date=_dt.date(2019, 1, 1),
                     event_type="buy", shares=100, unit_price=Decimal("8.05"), amount=Decimal("80.5")),
        IncomeStream(entity_id=h.id, stream_type="rent", group_key="斯帕出租屋",
                     currency="BEF", year=1974, amount=Decimal("3.2"), label="惠民租房"),
    ])
    session.commit()
    return h.id


def test_entity_assets(client):
    c, Session = client
    with Session() as s:
        eid = _seed(s)
    r = c.get(f"/api/v1/entities/{eid}/assets")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["name"] == "Henri Peeters"
    assert len(d["accounts"]) == 1
    acct = d["accounts"][0]
    assert acct["currency"] == "BEF" and acct["balance"] == 1050.0   # 末流水余额
    assert len(d["initial_assets"]) == 1
    assert d["initial_assets"][0]["asset_type"] == "bond"
    assert len(d["holdings"]) == 1
    assert d["holdings"][0]["company"] == "GE"
    assert len(d["income"]) == 1
    assert d["income"][0]["stream_type"] == "rent"


def test_entity_assets_404(client):
    c, _ = client
    assert c.get("/api/v1/entities/999999/assets").status_code == 404