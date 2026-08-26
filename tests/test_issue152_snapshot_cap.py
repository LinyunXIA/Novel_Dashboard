"""issue #152 回归：ingest CLI 快照重建不再硬编码 range(…,2026)，覆盖至 CALENDAR_MAX_YEAR。

#141 修复时 main.py 四处 rebuild_snapshots 仍显式传死区间，绕过 config.calendar_years()——
今年 2026 → 动态上限 2027，CLI 链路快照止于 2025。本文件验证：
1. main.py 源码不再出现 `range(1947, 2026)` / `range(from_year, 2026)` 字面量；
2. rebuild_snapshots（years 缺省）在 monkeypatch 后的上限年照常写行；
3. /overview 返回 calendar 边界供前端日历动态收敛。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, Integer, create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.core.snapshot as snapshot_mod
from app.config import CALENDAR_MAX_YEAR
from app.core.snapshot import rebuild_snapshots
from app.db import Base
from app.model import Account, Entity, LedgerEntry


MAIN_PY = Path(__file__).resolve().parents[1] / "app" / "ingest" / "main.py"


def test_main_py_no_hardcoded_snapshot_range():
    src = MAIN_PY.read_text(encoding="utf-8")
    assert "range(1947, 2026)" not in src
    assert "range(from_year, 2026)" not in src


@pytest.fixture
def session():
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


def _seed_account_with_flow(session, year: int):
    e = Entity(entity_type="person", name="SnapCap")
    session.add(e)
    session.flush()
    acc = Account(entity_id=e.id, currency="EUR")
    session.add(acc)
    session.flush()
    session.add(LedgerEntry(account_id=acc.id, date=date(year, 6, 1),
                            reason="流入", inflow=500, balance=500, kind="income"))
    session.flush()
    return acc


def test_rebuild_snapshots_covers_patched_cap_year(session, monkeypatch):
    """上限年（monkeypatch 到 2028）有流水的账户必须产出该年 account:* 快照行。"""
    monkeypatch.setattr("app.config.CALENDAR_MAX_YEAR", 2028)
    acc = _seed_account_with_flow(session, 2028)

    r = rebuild_snapshots(session)   # years 缺省 → calendar_years() → 上限 2028

    row = session.execute(select(snapshot_mod.Snapshot).where(
        snapshot_mod.Snapshot.scope == f"account:{acc.id}:EUR",
        snapshot_mod.Snapshot.as_of_year == 2028,
        snapshot_mod.Snapshot.as_of_date.is_(None))).scalar_one_or_none()
    assert row is not None and float(row.value) == 500.0
    assert r["accounts"] >= 1


def test_rebuild_snapshots_from_year_incremental(session):
    """from_year 增量：起点前旧段保留、起点起重建（§9.2c），且不写死上限年。"""
    acc = _seed_account_with_flow(session, 1990)
    rebuild_snapshots(session, from_year=1990)
    n_first = len(session.execute(select(snapshot_mod.Snapshot).where(
        snapshot_mod.Snapshot.scope == f"account:{acc.id}:EUR")).scalars().all())
    # 上限年随 config 推进：1990..CALENDAR_MAX_YEAR 每年一行（有余额年份）
    assert n_first == CALENDAR_MAX_YEAR - 1990 + 1


def test_overview_exposes_calendar_bounds(db_session):
    from app.api.app import app
    from app.api.deps import get_db
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        with TestClient(app) as c:
            ov = c.get("/api/v1/overview").json()
        assert ov["calendar"]["min_year"] == 1947
        assert ov["calendar"]["max_year"] == CALENDAR_MAX_YEAR
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def db_session():
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()
