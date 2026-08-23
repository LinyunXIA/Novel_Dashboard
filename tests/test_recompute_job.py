"""Unit tests for app/core/recompute 的 job/notification 接线（issue #13 回归）。

通过 SQLite 内存 + 临时 DDL 验证：
- register_job 写 recompute_job(status=done)
- record_recompute_done 写 job + recompute-done Notification，且字段正确
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.model import RecomputeJob, Notification
from app.core.recompute import register_job, record_recompute_done


@pytest.fixture
def session():
    """内存 SQLite + 临时 DDL（BigInteger PK → Integer 兼容 SQLite）。"""
    from sqlalchemy import BigInteger, Integer

    def _patch():
        for table in Base.metadata.tables.values():
            for col in table.columns:
                if isinstance(col.type, BigInteger) and col.primary_key:
                    col.type = Integer()

    _patch()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


class TestRegisterJob:
    def test_writes_job_done(self, session):
        jid = register_job(session, 1980, reason="test", files=["a.md", "b.md"])
        session.commit()
        job = session.get(RecomputeJob, jid)
        assert job is not None
        assert job.status == "done"
        assert job.start_year == 1980
        assert job.reason == "test"
        assert {f for f in (job.files or [])} >= {"a.md", "b.md"}
        assert job.finished_at is not None


class TestRecordRecomputeDone:
    def test_writes_job_and_notification(self, session):
        res = record_recompute_done(session, 1985, reason="ingest", files=["时间线.md"])
        session.commit()
        # job
        job = session.get(RecomputeJob, res["job_id"])
        assert job is not None and job.status == "done"
        # notification
        notif = session.get(Notification, res["notification_id"])
        assert notif is not None
        assert notif.kind == "recompute-done"
        assert notif.job_id == job.id
        assert "1985" in (notif.message or "")
        assert notif.payload.get("start_year") == 1985
        # 未读
        assert notif.read_at is None

    def test_default_reason(self, session):
        res = record_recompute_done(session, 2001)
        session.commit()
        notif = session.get(Notification, res["notification_id"])
        assert notif is not None
        assert notif.kind == "recompute-done"