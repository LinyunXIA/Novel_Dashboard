"""F-P2-01 事件·电影单测：解析器 + import + API link/unlink（§19.6）。"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.app import app
from app.api.deps import get_db
from app.db import Base
from app.ingest.writer import import_movie_events
from app.model import Account, Entity, LedgerEntry, MovieEvent


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


# issue #128：fixture 入库副本（源文件在 Design_Folder，不入 git）
TEST_RES = Path(__file__).resolve().parent / "fixtures/事件/电影/泰坦尼克.md"


def test_parse_titanic():
    from app.ingest.parsers.event_movie import parse_event_movie
    rec = parse_event_movie(TEST_RES)[0]
    assert rec["title"] == "泰坦尼克号"
    assert rec["currency"] == "USD" and rec["region"] == "NA+OS"
    assert rec["investment_total"] == pytest.approx(90_000_000.0)
    assert rec["principal_return_amount"] == pytest.approx(90_000_000.0)
    assert rec["principal_return_date"].isoformat() == "1998-09-01"
    assert rec["dividends_total"] == pytest.approx(376_740_000.0)


def test_import_upsert(db):
    from app.ingest.parsers.event_movie import parse_event_movie
    recs = parse_event_movie(TEST_RES)
    r1 = import_movie_events(db, recs); db.commit()
    assert r1["inserted"] == 1
    r2 = import_movie_events(db, recs); db.commit()
    assert r2["skipped"] == 1                      # 幂等 upsert
    row = db.execute(select(MovieEvent)).scalar_one()
    assert row.title == "泰坦尼克号" and float(row.dividends_total) == pytest.approx(376_740_000.0)


def test_parse_one_now_reaches_movie_parser():
    """移除 Phase 2 早 return 后，event_movie 应真进到 parser（不再跳过）。"""
    from app.ingest.parse import parse_one
    pr = parse_one("基准/事件/电影/泰坦尼克.md", TEST_RES)
    assert pr.category == "event_movie" and pr.records and pr.records[0]["title"] == "泰坦尼克号"


class TestApiLink:
    def _seed(self, db):
        db.add_all([Entity(entity_type="person", name="Stijn"),
                    Account(entity_id=1, currency="USD")])
        from app.ingest.parsers.event_movie import parse_event_movie
        import_movie_events(db, parse_event_movie(TEST_RES))
        db.commit()

    def test_link_writes_ledger_idempotent(self, db):
        self._seed(db)
        app.dependency_overrides[get_db] = lambda: db
        try:
            acc = db.execute(select(Account)).scalar_one()
            with TestClient(app) as c:
                lst = c.get("/api/v1/movie-events").json()["items"]
                mid = lst[0]["id"]
                r = c.post(f"/api/v1/movie-events/{mid}/link", json={"account_id": acc.id})
                assert r.status_code == 200 and r.json()["ledger_written"] >= 2
                assert r.json()["skipped"] is False
                # 幂等：再次 link → skipped
                r2 = c.post(f"/api/v1/movie-events/{mid}/link", json={"account_id": acc.id})
                assert r2.json()["skipped"] is True
                # ledger 写入了（本金返还 income + 分红 investment_income；投资出缺日期跳过）
                kinds = {e.kind for e in db.execute(select(LedgerEntry)).scalars()}
                assert {"income", "investment_income"} <= kinds
                # linked 标记
                m = db.execute(select(MovieEvent)).scalar_one()
                assert m.linked_account_id == acc.id
                # unlink
                assert c.post(f"/api/v1/movie-events/{mid}/unlink").json()["unlinked"] is True
        finally:
            app.dependency_overrides.pop(get_db, None)

# ---- 七轮审计 #184：分红-only 事件的快照起点 ----
def test_dividend_only_link_rebuilds_from_dividend_year(db):
    """仅分红流的电影：link 后 rebuild_snapshots 起点=分红日期年（非 today 回退）。"""
    from datetime import date as _d
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from app.core.snapshot import rebuild_snapshots
    from app.model import Account, Snapshot
    e = Entity(entity_type="person", name="D184人")
    db.add(e); db.flush()
    acc = Account(entity_id=e.id, currency="USD")
    db.add(acc); db.flush()
    m = MovieEvent(title="分红only", currency="USD",
                   investment_date=None, investment_total=None,
                   principal_return_date=_d(2003, 6, 1), dividends_total=7.0)
    db.add(m); db.commit()
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as c:
            r = c.post(f"/api/v1/movie-events/{m.id}/link", json={"account_id": acc.id})
            assert r.status_code == 200 and r.json()["ledger_written"] == 1
        # 分红年份(2003)的 entity 快照行已反映该笔收入
        row = db.execute(select(Snapshot).where(
            Snapshot.scope == f"entity:{e.id}:USD",
            Snapshot.as_of_year == 2003)).scalar_one_or_none()
        assert row is not None and abs(float(row.value) - 7.0) < 0.01
    finally:
        app.dependency_overrides.pop(get_db, None)
