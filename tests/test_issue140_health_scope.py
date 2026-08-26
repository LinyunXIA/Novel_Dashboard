"""issue #140 回归：健康复核范围化（§9.2d）+ findings 持久化进通知 + 异常不静默。

- run_report/summarize 支持 from_year：H1/H2/H4/负余额只报告该年及以后；
- record_recompute_done payload 携带 health_findings（crit 优先，截断 20 条）与
  health_findings_total；health 自身异常 → payload["health_error"]（不再吞成 {}）。
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import BigInteger, Integer, create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core import health as health_mod
from app.core.health import run_report, summarize
from app.core.recompute import record_recompute_done
from app.db import Base
from app.model import (Account, Entity, IncomeStream, LedgerEntry, Notification,
                       TimelineEvent)


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


def _seed(session):
    e = Entity(entity_type="person", name="Henri")
    session.add(e)
    session.flush()
    acc = Account(entity_id=e.id, currency="BEF", bank=None)
    session.add(acc)
    session.flush()
    # 一早一晚两笔负余额（H4/warn）
    session.add(LedgerEntry(account_id=acc.id, date=date(1975, 12, 30),
                            reason="早年透支", outflow=100, balance=-100, kind="expense"))
    session.add(LedgerEntry(account_id=acc.id, date=date(2020, 12, 30),
                            reason="近年透支", outflow=50, balance=-50, kind="expense"))
    # 一早一晚两条「无收益流年份」时间线条目（H1/warn）
    session.add(TimelineEvent(event_year=1950, title="早年事件", source_file="t.md"))
    session.add(TimelineEvent(event_year=2020, title="近年事件", source_file="t.md"))
    # H1 需要 income_years 非空才参与比对：给一个 1990 年收益流
    session.add(IncomeStream(entity_id=e.id, stream_type="salary", group_key="g",
                             currency="BEF", year=1990, amount=10, label="l",
                             source_file="s.md"))
    session.flush()


def test_run_report_scopes_by_from_year(session):
    _seed(session)
    full = run_report(session)
    assert {f["location"] for f in full if f["rule"] == "H1"} >= {
        "时间线 1950「早年事件」", "时间线 2020「近年事件」"}
    neg_years_full = sorted(f["location"] for f in full
                            if f["level"] == "warn" and "负余额" in f["detail"])
    assert len(neg_years_full) == 2

    scoped = run_report(session, from_year=2000)
    h1_locs = [f["location"] for f in scoped if f["rule"] == "H1"]
    assert h1_locs == ["时间线 2020「近年事件」"]          # 早年条目被范围滤除
    neg = [f for f in scoped if "负余额" in f["detail"]]
    assert len(neg) == 1 and "2020" in neg[0]["location"]

    s_full = summarize(session)
    s_scope = summarize(session, from_year=2000)
    assert s_full["H1"]["total"] > s_scope["H1"]["total"]


def test_record_recompute_done_persists_findings(session):
    _seed(session)
    out = record_recompute_done(session, start_year=2000, reason="test")
    n = session.get(Notification, out["notification_id"])
    p = n.payload
    assert p["start_year"] == 2000
    assert isinstance(p["health"], dict) and "H4" in p["health"]
    assert p["health_findings_total"] >= 1
    assert any("负余额" in f["detail"] for f in p["health_findings"])
    # crit 优先排序：若混有 crit，则其排前（此处仅 warn 也须合法）
    levels = [f["level"] for f in p["health_findings"]]
    assert levels == sorted(levels, key=lambda x: 0 if x == "crit" else 1)


def test_record_recompute_done_does_not_swallow_errors(session, monkeypatch):
    _seed(session)

    def boom(s, from_year=None):
        raise RuntimeError("health exploded")

    monkeypatch.setattr(health_mod, "run_report", boom)
    out = record_recompute_done(session, start_year=1990, reason="test")
    n = session.get(Notification, out["notification_id"])
    assert "health exploded" in n.payload["health_error"]
    assert n.payload["health"] == {}
