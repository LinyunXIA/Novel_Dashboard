"""issue #155：补齐 §14.2 两端点——GET /snapshots/{date}、GET /source-files/{id}/versions/{vid}。"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.app import app
from app.api.deps import get_db
from app.config import CALENDAR_MAX_YEAR
from app.db import Base
from app.model import Account, Entity, LedgerEntry, SourceFileVersion


@pytest.fixture
def db():
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


class TestSnapshotByDate:
    def test_snapshot_at_date_returns_account_scopes(self, db):
        e = Entity(entity_type="person", name="Snap155")
        db.add(e)
        db.flush()
        acc = Account(entity_id=e.id, currency="EUR")
        db.add(acc)
        db.flush()
        db.add(LedgerEntry(account_id=acc.id, date=date(1990, 3, 1),
                           reason="流入", inflow=800, balance=800, kind="income"))
        db.flush()
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as c:
                r = c.get("/api/v1/snapshots/1990-12-30")
                assert r.status_code == 200
                scopes = [x["scope"] for x in r.json()]
                assert f"account:{acc.id}:EUR" in scopes
                # scope 过滤
                r2 = c.get("/api/v1/snapshots/1990-12-30",
                           params={"scope": f"account:{acc.id}:EUR"})
                assert all(x["scope"] == f"account:{acc.id}:EUR" for x in r2.json())
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_snapshot_at_date_out_of_calendar_422(self, db):
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as c:
                assert c.get(f"/api/v1/snapshots/{CALENDAR_MAX_YEAR + 1}-01-01").status_code == 422
                assert c.get("/api/v1/snapshots/1946-12-31").status_code == 422
        finally:
            app.dependency_overrides.pop(get_db, None)


class TestSourceFileVersionContent:
    def test_version_content_full(self, db):
        db.add(SourceFileVersion(file_path="经济/银行/a.md", version=1,
                                 content="v1-full-content", is_current=False))
        db.add(SourceFileVersion(file_path="经济/银行/a.md", version=2,
                                 content="v2-full-content", is_current=True))
        db.flush()
        vid = db.query(SourceFileVersion).filter_by(version=1).one().id
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as c:
                r = c.get(f"/api/v1/source-files/{vid}/versions/2")
                assert r.status_code == 200
                body = r.json()
                # 完整内容（非 120 字 preview）
                assert body["content"] == "v2-full-content"
                assert body["version"] == 2 and body["current"] is True and body["file"] == "经济/银行/a.md"
                # 不存在版本号 → 404
                assert c.get(f"/api/v1/source-files/{vid}/versions/9").status_code == 404
                # 文件标识本身不存在 → 404
                assert c.get("/api/v1/source-files/999999/versions/1").status_code == 404
        finally:
            app.dependency_overrides.pop(get_db, None)
