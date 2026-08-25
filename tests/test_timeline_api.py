"""F-P2-05 编年史 API 通路单测（DESIGN §12/§6.4）。"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.app import app
from app.api.deps import get_db
from app.db import Base
from app.model import TimelineEvent, UserDataOverlay


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


def _seed_source(db, year=1990, title="源条", note="源备注"):
    db.add(TimelineEvent(event_year=year, title=title, note=note, overlay=False,
                         source_file="时间线.md"))
    db.flush()


def _seed_system(db):
    db.add(TimelineEvent(event_year=1990, title="投资赎回", note="…(inv#7)", overlay=True,
                         source_file=None))
    db.flush()


def _tl(db, **kw):
    return db.execute(select(TimelineEvent).filter_by(**kw)).scalars().all()


class TestTimelineApi:
    def test_post_create_and_merged_get(self, db):
        _seed_source(db)
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as c:
                r = c.post("/api/v1/timeline-events",
                           json={"event_year": 1990, "event_date": "1990-06-01",
                                 "title": "源条", "note": "改后", "decade": None})
                assert r.status_code == 201 and r.json()["idempotent"] is False
                # 合并 GET：每 key 一行，覆盖行优先（可编辑），源标 has_source
                lst = c.get("/api/v1/timeline-events?page_size=100").json()["items"]
                assert len(lst) == 1 and lst[0]["title"] == "源条"
                assert lst[0]["editable"] is True and lst[0]["has_source"] is True
                # 覆盖行 note=改后（优先显示）
                assert lst[0]["note"] == "改后"
                # diff
                d = c.get("/api/v1/timeline-events/overlay/diff").json()["items"]
                assert any(x["key"] == "1990:源条" for x in d)
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_patch_update(self, db):
        _seed_source(db)
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as c:
                c.post("/api/v1/timeline-events",
                       json={"event_year": 1990, "title": "源条", "note": "a"})
                cov = _tl(db, overlay=True)[0]
                r = c.patch(f"/api/v1/timeline-events/{cov.id}", json={"note": "b"})
                assert r.status_code == 200
                row = db.get(TimelineEvent, cov.id)
                assert row.note == "b"
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_delete_source_reemerges_and_guard(self, db):
        _seed_source(db)
        _seed_system(db)
        app.dependency_overrides[get_db] = lambda: db
        try:
            src = _tl(db, overlay=False)[0]
            sysrow = _tl(db, overlay=True, source_file=None)[0]
            with TestClient(app) as c:
                # guard：对纯源行 DELETE → 404
                assert c.delete(f"/api/v1/timeline-events/{src.id}").status_code == 404
                # guard：对系统行 DELETE → 404（issue #86 不可删）
                assert c.delete(f"/api/v1/timeline-events/{sysrow.id}").status_code == 404
                # 创建覆盖 → 删除 → 源重新生效
                c.post("/api/v1/timeline-events",
                       json={"event_year": 1990, "title": "源条", "note": "改"})
                cov = _tl(db, overlay=True, source_file="overlay:timeline:1990:源条")[0]
                r = c.delete(f"/api/v1/timeline-events/{cov.id}")
                assert r.status_code == 200 and r.json()["source_preserved"] is True
                # 覆盖行已删，源行仍在
                assert not _tl(db, overlay=True, source_file="overlay:timeline:1990:源条")
                assert _tl(db, overlay=False, title="源条")
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_restore_and_source_as_latest(self, db):
        _seed_source(db, title="A", note="源备注")
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as c:
                c.post("/api/v1/timeline-events", json={"event_year": 1990, "title": "A", "note": "改"})
                cov = _tl(db, overlay=True)[0]
                # restore
                r = c.post(f"/api/v1/timeline-events/{cov.id}/overlay/restore")
                assert r.status_code == 200 and r.json()["source_preserved"] is True
                assert not _tl(db, overlay=True, source_file="overlay:timeline:1990:A")
                # 重新创建 → source_as_latest 吸收源备注
                c.post("/api/v1/timeline-events", json={"event_year": 1990, "title": "A", "note": "改2"})
                cov2 = _tl(db, overlay=True)[0]
                r2 = c.post(f"/api/v1/timeline-events/{cov2.id}/overlay/source-as-latest")
                assert r2.json()["status"] == "synced"
                o = db.execute(select(UserDataOverlay).where(
                    UserDataOverlay.key == "1990:A")).scalar_one()
                assert o.payload["note"] == "源备注"
                # 对纯源行 restore → 404（guard）
                src = _tl(db, overlay=False)[0]
                assert c.post(f"/api/v1/timeline-events/{src.id}/overlay/restore").status_code == 404
        finally:
            app.dependency_overrides.pop(get_db, None)