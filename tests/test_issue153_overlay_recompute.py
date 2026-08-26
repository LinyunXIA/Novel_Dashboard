"""issue #153：overlay restore / source-as-latest / merge 三端点触发增量重算（§12）。

直接增改删端点此前已走 _after_timeline_write；本轮补齐三个子动作端点的
重算+通知断言：recompute_job.start_year 口径 = 条目年份（merge 取覆盖层最小年）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.app import app
from app.api.deps import get_db
from app.db import Base
from app.model import Notification, RecomputeJob, TimelineEvent


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


def _last_job(db):
    return db.execute(
        select(RecomputeJob).order_by(RecomputeJob.id.desc()).limit(1)).scalar_one()


def _client(db):
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


class TestOverlayRecomputeTrigger:
    def test_restore_triggers_recompute_at_entry_year(self, db):
        _seed_source(db, year=1992)
        c = _client(db)
        try:
            with c:
                c.post("/api/v1/timeline-events",
                       json={"event_year": 1992, "title": "源条", "note": "改"})
                cov = db.execute(select(TimelineEvent).where(
                    TimelineEvent.overlay.is_(True))).scalars().one()
                r = c.post(f"/api/v1/timeline-events/{cov.id}/overlay/restore")
                assert r.status_code == 200 and r.json()["source_preserved"] is True
            job = _last_job(db)
            assert job.start_year == 1992 and job.status == "done"
            assert job.reason == "timeline-overlay"
            note = db.execute(select(Notification).order_by(Notification.id.desc())
                              .limit(1)).scalar_one()
            assert note.kind == "recompute-done" and note.payload["start_year"] == 1992
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_source_as_latest_synced_triggers(self, db):
        _seed_source(db, year=1988)
        c = _client(db)
        try:
            with c:
                c.post("/api/v1/timeline-events",
                       json={"event_year": 1988, "title": "源条", "note": "改"})
                cov = db.execute(select(TimelineEvent).where(
                    TimelineEvent.overlay.is_(True))).scalars().one()
                r = c.post(f"/api/v1/timeline-events/{cov.id}/overlay/source-as-latest")
                assert r.status_code == 200 and r.json()["status"] == "synced"
            assert _last_job(db).start_year == 1988
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_source_as_latest_no_source_no_trigger(self, db):
        """无源行 → status=no_source，生效内容未变 → 不产生新 recompute job。"""
        c = _client(db)
        try:
            with c:
                r = c.post("/api/v1/timeline-events",
                           json={"event_year": 1975, "title": "孤条", "note": None})
                cov_id = r.json()["timeline_event_id"]
                n_jobs = len(db.execute(select(RecomputeJob)).scalars().all())
                r2 = c.post(f"/api/v1/timeline-events/{cov_id}/overlay/source-as-latest")
                assert r2.status_code == 200 and r2.json()["status"] == "no_source"
            assert len(db.execute(select(RecomputeJob)).scalars().all()) == n_jobs
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_merge_triggers_with_min_overlay_year(self, db):
        db.add(TimelineEvent(event_year=1985, title="甲", overlay=False,
                             source_file="时间线.md"))
        db.add(TimelineEvent(event_year=1999, title="乙", overlay=False,
                             source_file="时间线.md"))
        db.flush()
        c = _client(db)
        try:
            with c:
                # 两个不同年份的覆盖条目；POST 各自触发一次（起点=各自年份）
                c.post("/api/v1/timeline-events", json={"event_year": 1999, "title": "乙"})
                c.post("/api/v1/timeline-events", json={"event_year": 1985, "title": "甲"})
                # 制造一个孤儿覆盖行（删 user 行留 timeline 覆盖行）→ merge 应 clean 并触发
                orphan = db.execute(select(TimelineEvent).where(
                    TimelineEvent.title == "乙", TimelineEvent.overlay.is_(True))).scalar_one()
                from app.model import UserDataOverlay
                o = db.execute(select(UserDataOverlay).where(
                    UserDataOverlay.key == "1999:乙")).scalar_one()
                db.delete(o)
                db.flush()
                before = len(db.execute(select(RecomputeJob)).scalars().all())
                r = c.post("/api/v1/timeline-events/overlay/merge")
                assert r.status_code == 200 and r.json()["cleaned"] >= 1
            jobs = db.execute(select(RecomputeJob)).scalars().all()
            assert len(jobs) == before + 1
            assert _last_job(db).start_year == 1985   # 最小覆盖条目年兜底
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_merge_noop_no_trigger(self, db):
        """无覆盖层且无孤儿 → merge no-op，不产生新 job。"""
        _seed_source(db)
        c = _client(db)
        try:
            with c:
                before = len(db.execute(select(RecomputeJob)).scalars().all())
                r = c.post("/api/v1/timeline-events/overlay/merge")
                assert r.status_code == 200
                assert r.json() == {"reconciled": 0, "cleaned": 0}
            assert len(db.execute(select(RecomputeJob)).scalars().all()) == before
        finally:
            app.dependency_overrides.pop(get_db, None)
