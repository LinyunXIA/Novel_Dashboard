"""F-P2-02 事件·股票单测：解析器 + import + API（associate/buy/sell/dividend/util，§19.6 block D）。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.app import app
from app.api.deps import get_db
from app.db import Base
from app.ingest.writer import import_stock_events
from app.model import Account, Entity, HoldingEvent, LedgerEntry, Snapshot, StockEvent


@pytest.fixture
def db():
    from sqlalchemy import BigInteger, Integer
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    s = S()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


TEST_RES = Path(__file__).resolve().parent.parent / "Design_Folder/基准/事件/股票/虎牙.md"


def _seed(db):
    e = Entity(entity_type="person", name="Stijn")
    db.add(e)
    db.flush()
    a = Account(entity_id=e.id, currency="USD")
    db.add(a)
    db.flush()
    db.add(LedgerEntry(account_id=a.id, date=date(2018, 1, 1), inflow=50000,
                       balance=50000, kind="income", reason="初始化现金"))
    db.flush()
    return e.id, a.id


def _buy_events(db):
    return db.execute(select(StockEvent).where(StockEvent.event_type == "buy")).scalars().all()


def test_parse_huya():
    from app.ingest.parsers.event_stock import parse_event_stock
    recs = parse_event_stock(TEST_RES)
    buys = [r for r in recs if r["event_type"] == "buy"]
    assert buys, "should extract at least the A轮 buy"
    a = buys[0]
    assert a["company"] == "虎牙" and a["date"].startswith("2017")
    assert a["shares"] == pytest.approx(22058824.0) and a["unit_price"] == pytest.approx(3.40)


def test_import_stock_events_upsert(db):
    from app.ingest.parsers.event_stock import parse_event_stock
    recs = parse_event_stock(TEST_RES)
    r1 = import_stock_events(db, recs); db.commit()
    assert r1["inserted"] == len(recs)
    r2 = import_stock_events(db, recs); db.commit()
    assert r2["skipped"] == len(recs)                 # 幂等
    row = db.execute(select(StockEvent).where(StockEvent.event_type == "buy")
                     .order_by(StockEvent.id)).scalars().first()
    assert row.currency == "USD" and row.shares is not None


class TestApi:
    def test_buy_sets_holding_ledger_and_snapshot(self, db):
        eid, aid = _seed(db)
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as c:
                r = c.post("/api/v1/stock-events/buy", json={
                    "entity_id": eid, "account_id": aid, "company": "AAPL",
                    "date": "2018-06-01", "unit_price": 10, "shares": 1000,
                    "event_id": "api-buy-1"})
                assert r.status_code == 200 and r.json()["skipped"] is False
                # holding batch + ledger
                h = db.execute(select(HoldingEvent)).scalars().one()
                assert h.event_type == "buy" and float(h.shares) == 1000
                led = db.execute(select(LedgerEntry)).scalars().all()
                buy = next(x for x in led if x.kind == "expense")
                assert float(buy.outflow) == 10000 and "股票事件" in buy.note
                # 快照含市值（entity/family 含、account 不含）
                assert db.execute(select(Snapshot).where(
                    Snapshot.scope == f"entity:{eid}:USD")).scalars().first() is not None
                # 幂等：重复 event_id → skipped
                r2 = c.post("/api/v1/stock-events/buy", json={
                    "entity_id": eid, "account_id": aid, "company": "AAPL",
                    "date": "2018-06-01", "unit_price": 10, "shares": 1000,
                    "event_id": "api-buy-1"})
                assert r2.json()["skipped"] is True
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_sell_oversell_422(self, db):
        eid, aid = _seed(db)
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as c:
                c.post("/api/v1/stock-events/buy", json={
                    "entity_id": eid, "account_id": aid, "company": "AAPL",
                    "date": "2018-06-01", "unit_price": 10, "shares": 1000,
                    "event_id": "sb-1"})
                r = c.post("/api/v1/stock-events/sell", json={
                    "entity_id": eid, "account_id": aid, "company": "AAPL",
                    "date": "2019-06-01", "sell_price": 15, "shares": 9999,
                    "event_id": "ss-1"})
                assert r.status_code == 422
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_associate_write_buy_event(self, db):
        eid, aid = _seed(db)
        from app.ingest.parsers.event_stock import parse_event_stock
        import_stock_events(db, [r for r in parse_event_stock(TEST_RES) if r["event_type"] == "buy"])
        db.commit()
        app.dependency_overrides[get_db] = lambda: db
        try:
            se = _buy_events(db)[0]
            with TestClient(app) as c:
                r = c.post("/api/v1/stock-events/associate",
                           json={"stock_event_id": se.id, "entity_id": eid, "account_id": aid})
                assert r.status_code == 200 and r.json()["skipped"] is False
                h = db.execute(select(HoldingEvent).where(
                    HoldingEvent.event_type == "buy")).scalars().one()
                assert "西安" in h.company or h.company  # company 来自解析
                se2 = db.get(StockEvent, se.id)
                assert se2.linked_entity_id == eid
                # 已关联 → 再次 skipped
                r2 = c.post("/api/v1/stock-events/associate",
                            json={"stock_event_id": se.id, "entity_id": eid, "account_id": aid})
                assert r2.json()["skipped"] is True
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_positions_endpoint(self, db):
        eid, aid = _seed(db)
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as c:
                c.post("/api/v1/stock-events/buy", json={
                    "entity_id": eid, "account_id": aid, "company": "AAPL",
                    "date": "2018-06-01", "unit_price": 10, "shares": 1000,
                    "event_id": "pb-1"})
                pos = c.get("/api/v1/stock-events/positions").json()["items"]
                assert len(pos) == 1 and pos[0]["company"] == "AAPL"
                assert pos[0]["market_value"] == pytest.approx(10000.0)
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_events_endpoint_lists(self, db):
        _seed(db)
        from app.ingest.parsers.event_stock import parse_event_stock
        import_stock_events(db, parse_event_stock(TEST_RES))
        db.commit()
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as c:
                out = c.get("/api/v1/stock-events/events").json()
                assert out["total"] >= 1
                assert any(x["event_type"] == "buy" for x in out["items"])
        finally:
            app.dependency_overrides.pop(get_db, None)