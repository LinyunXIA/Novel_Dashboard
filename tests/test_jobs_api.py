"""issue #138 回归：异步 job 资源（import-jobs / recompute-jobs · DESIGN §14.2 方案A）。

- POST 建 pending → 后台执行（BackgroundTasks 在 TestClient 内同步跑完）→ done；
- recompute job 复用任务行收尾 + 通知携带健康摘要；GET /{id} 暴露 health；
- import job：provider 白名单 422 / 假 runner 全链 done(result)；
- DELETE 仅 pending 可取消（204→409），404 语义。
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.core.jobs as jobs_mod
from app.api.app import app
from app.api.deps import get_db
from app.db import Base
from app.model import Account, Entity, LedgerEntry, Notification, RecomputeJob


@pytest.fixture
def env():
    from sqlalchemy import BigInteger, Integer
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def _override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    # 后台执行器注入同一 SQLite 工厂（替代 make_sessionmaker(env) 的真实 PG）
    jobs_mod.make_sessionmaker = lambda env: Session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c, Session
    app.dependency_overrides.clear()
    engine.dispose()


def test_recompute_job_full_lifecycle(env):
    c, Session = env
    s = Session()
    e = Entity(entity_type="person", name="Henri")
    s.add(e)
    s.flush()
    acc = Account(entity_id=e.id, currency="BEF", bank=None)
    s.add(acc)
    s.flush()
    s.add(LedgerEntry(account_id=acc.id, date=date(1990, 12, 30), reason="x",
                      inflow=100, balance=100, kind="income"))
    s.commit()
    s.close()

    r = c.post("/api/v1/recompute-jobs", json={"start_year": 1980, "reason": "test"})
    assert r.status_code == 202
    jid = r.json()["id"]
    assert r.json()["status"] == "pending"

    detail = c.get(f"/api/v1/recompute-jobs/{jid}").json()
    assert detail["status"] == "done"
    assert detail["health"] is not None and "H4" in detail["health"]

    # 任务行复用：不产生第二个 job 行；通知挂本 job
    s2 = Session()
    assert s2.get(RecomputeJob, jid).status == "done"
    n = s2.query(Notification).filter(Notification.job_id == jid).one_or_none()
    assert n is not None and n.kind == "recompute-done"
    s2.close()

    lst = c.get("/api/v1/recompute-jobs", params={"status": "done"}).json()
    assert any(x["id"] == jid for x in lst["items"])


def test_import_job_provider_whitelist_and_fake_runner(env, monkeypatch):
    c, _Session = env

    r = c.post("/api/v1/import-jobs", json={"provider": "nope"})
    assert r.status_code == 422

    def fake_runner(s, payload):
        return {"stats": {"companies": 2}}

    monkeypatch.setattr(jobs_mod, "_default_runner", lambda provider: fake_runner)
    r = c.post("/api/v1/import-jobs", json={"provider": "company-info", "payload": {}})
    assert r.status_code == 202
    jid = r.json()["id"]
    detail = c.get(f"/api/v1/import-jobs/{jid}").json()
    assert detail["status"] == "done"
    assert detail["result"] == {"stats": {"companies": 2}}
    assert detail["error"] is None


def test_import_job_failure_marks_failed(env, monkeypatch):
    c, Session = env

    def boom(s, payload):
        raise RuntimeError("外部系统不可达")

    monkeypatch.setattr(jobs_mod, "_default_runner", lambda provider: boom)
    r = c.post("/api/v1/import-jobs", json={"provider": "labor-cost",
                                            "payload": {"year": 2020}})
    jid = r.json()["id"]
    detail = c.get(f"/api/v1/import-jobs/{jid}").json()
    assert detail["status"] == "failed"
    assert "RuntimeError" in (detail["error"] or "")


def test_delete_cancel_semantics(env):
    c, Session = env
    # pending → 取消成功
    s = Session()
    j = RecomputeJob(start_year=1990, reason="t", status="pending")
    s.add(j)
    s.commit()
    jid = j.id
    s.close()
    assert c.delete(f"/api/v1/recompute-jobs/{jid}").status_code == 204
    # 已取消（failed）→ 再删 409
    assert c.delete(f"/api/v1/recompute-jobs/{jid}").status_code == 409
    # 不存在 → 404
    assert c.delete("/api/v1/recompute-jobs/99999").status_code == 404
