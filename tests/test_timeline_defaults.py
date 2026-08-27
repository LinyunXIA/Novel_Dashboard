"""F-P2+-04 时间线默认事件生成测试（app/core/timeline_defaults.py）。

验证四类默认事件生成（首次建仓/影视首次/股票事件首次/每年 R1-5 投资）、幂等重跑合并、
`--rebuild` 清旧重建且不触碰 source_file!=derive 的手工/overlay 行。
内存 SQLite（BigInteger 主键降级 Integer，模式见 tests/test_ingest_idempotency.py）。
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

import pytest
from sqlalchemy import BigInteger, Integer, create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.timeline_defaults import SOURCE, derive_default_timeline
from app.model import (
    Base, Entity, HoldingEvent, Investment, InvestmentAlloc,
    MovieEvent, StockEvent, TimelineEvent,
)


@pytest.fixture()
def session():
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


def _add_all(session, rows):
    session.add_all(rows)
    session.commit()


def _seed(session):
    ent = Entity(entity_type="company", name="Peeters Capital Mgmt")
    session.add(ent)
    session.flush()
    _add_all(session, [
        HoldingEvent(entity_id=ent.id, company="APPL", date=_dt.date(1989, 1, 15),
                     event_type="buy", shares=100),
        MovieEvent(title="泰坦尼克号", investment_date=_dt.date(1997, 1, 1)),
        StockEvent(company="MSFT", date=_dt.date(2001, 1, 1), event_type="buy"),
        Investment(year=1989, region="欧洲", risk_lvl="R5", start_date=_dt.date(1989, 1, 15)),
    ])
    inv = session.execute(select(Investment)).scalar_one()
    _add_all(session, [
        InvestmentAlloc(investment_id=inv.id, entity_id=ent.id, currency="USD", amount=Decimal("1000")),
    ])


def _titles(session):
    return {t.title for t in session.execute(select(TimelineEvent)).scalars()}


def test_generates_four_default_kinds(session):
    _seed(session)
    r = derive_default_timeline(session)
    assert r["inserted"] == 4
    titles = _titles(session)
    assert "「Peeters Capital Mgmt」首次建仓 APPL" in titles
    assert "投资《泰坦尼克号》" in titles
    assert "「MSFT」首次 buy" in titles
    assert "1989 年 欧洲 R5 投资" in titles
    # 标记与落地字段
    ev = session.execute(select(TimelineEvent)).scalars().all()
    assert all(e.source_file == SOURCE and e.overlay is False for e in ev)
    assert all(e.decade == f"{(e.event_year // 10) * 10}s" for e in ev)


def test_idempotent_rerun_merges_no_dupe(session):
    _seed(session)
    derive_default_timeline(session)
    n1 = len(session.execute(select(TimelineEvent)).scalars().all())
    r = derive_default_timeline(session)
    assert r["inserted"] == 0 and r["total"] == 4
    n2 = len(session.execute(select(TimelineEvent)).scalars().all())
    assert n1 == n2 == 4


def test_rebuild_keeps_only_derived_and_manual_survives(session):
    _seed(session)
    derive_default_timeline(session)
    manual = TimelineEvent(event_year=1974, title="手写占位事件", note="不走默认",
                           source_file="手工.md", overlay=False)
    session.add(manual)
    session.commit()
    r = derive_default_timeline(session, rebuild=True)
    assert r["total"] == 4
    titles = _titles(session)
    assert "手写占位事件" in titles          # 手工行保留
    t = session.execute(select(TimelineEvent)).scalars().all()
    assert len(t) == 5                        # 4 默认(重建) + 1 手工


def test_empty_db_generates_nothing(session):
    r = derive_default_timeline(session)
    assert r["total"] == 0 and r["inserted"] == 0