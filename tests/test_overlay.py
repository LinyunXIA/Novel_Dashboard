"""F-P2-05 编年史覆盖层服务层单测（DESIGN §12/§6.4）。"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.overlay import (create_overlay, delete_overlay, diff_overlay, list_user_overlays,
                              make_key, merge_overlay, restore_overlay, source_as_latest,
                              update_overlay)
from app.db import Base
from app.model import TimelineEvent, UserDataOverlay


@pytest.fixture
def session():
    from sqlalchemy import BigInteger, Integer
    from sqlalchemy.pool import StaticPool
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


def _seed_source(session, year=1990, title="源条", note="源备注"):
    session.add(TimelineEvent(event_year=year, title=title, note=note, overlay=False,
                              source_file="时间线.md"))
    session.flush()


def _seed_system(session, year=1990, title="投资赎回"):
    session.add(TimelineEvent(event_year=year, title=title, note="…(inv#7)", overlay=True,
                              source_file=None))     # issue #86 系统行
    session.flush()


def _tl_overlay(session, key):
    return session.execute(select(TimelineEvent).where(
        TimelineEvent.overlay.is_(True),
        TimelineEvent.source_file == f"overlay:timeline:{key}")).scalars().first()


def test_create_idempotent(session):
    _seed_source(session)
    r1 = create_overlay(session, event_year=1990, event_date=date(1990, 6, 1), title="源条", note="改后")
    r2 = create_overlay(session, event_year=1990, event_date=date(1990, 6, 1), title="源条", note="改后2")
    assert r1["idempotent"] is False and r2["idempotent"] is True
    assert len(list_user_overlays(session)) == 1
    assert session.execute(select(TimelineEvent).where(
        TimelineEvent.source_file.like("overlay:timeline:%"))).scalars().all().__len__() == 1


def test_update_inplace_and_rekey_migrate(session):
    _seed_source(session)
    create_overlay(session, event_year=1990, event_date=date(1990, 6, 1), title="源条", note="n1")
    r = update_overlay(session, "1990:源条", note="n2")
    assert r["key"] == "1990:源条" and r["migrated"] is False
    o = session.execute(select(UserDataOverlay).where(
        UserDataOverlay.key == "1990:源条")).scalar_one()
    assert o.payload["note"] == "n2"
    # title 变 → 迁移到新 key
    r2 = update_overlay(session, "1990:源条", title="新标题")
    assert r2["migrated"] is True and r2["key"] == "1990:新标题"
    assert [o.key for o in list_user_overlays(session)] == ["1990:新标题"]
    assert _tl_overlay(session, "1990:源条") is None        # 旧覆盖行已迁走
    assert _tl_overlay(session, "1990:新标题") is not None


def test_delete_preserves_source(session):
    _seed_source(session)
    create_overlay(session, event_year=1990, title="源条", note="改")
    r = delete_overlay(session, "1990:源条")
    assert r["source_preserved"] is True and r["deleted"] >= 1
    # 源行仍在（overlay=False）
    assert session.execute(select(TimelineEvent).where(
        TimelineEvent.overlay.is_(False), TimelineEvent.title == "源条")).scalars().one()


def test_merge_reconcile_and_cleanup(session):
    _seed_source(session)
    create_overlay(session, event_year=1990, title="源条", note="改")
    # 孤儿覆盖行（无 user 行）应被清
    session.add(TimelineEvent(event_year=2000, title="孤儿", note="x", overlay=True,
                              source_file="overlay:timeline:2000:孤儿"))
    session.flush()
    r = merge_overlay(session)
    assert r["reconciled"] >= 1 and r["cleaned"] >= 1
    keys = [o.key for o in list_user_overlays(session)]
    assert "1990:源条" in keys and "2000:孤儿" not in keys
    assert _tl_overlay(session, "2000:孤儿") is None


def test_diff_statuses(session):
    _seed_source(session, year=1990, title="A", note="原")
    # new（无源）
    create_overlay(session, event_year=2020, title="全新", note="x")
    # modified（note 变）
    create_overlay(session, event_year=1990, title="A", note="改过")
    # unchanged（与源一致）
    create_overlay(session, event_year=1990, title="A", note="原")   # 覆盖行先改后改回 → 实际同 key 只一条
    diffs = {d["key"]: d for d in diff_overlay(session)}
    assert diffs["1990:A"]["status"] in ("modified", "unchanged")
    assert diffs["2020:全新"]["status"] == "new"


def test_restore_equals_delete(session):
    _seed_source(session)
    create_overlay(session, event_year=1990, title="源条", note="改")
    r = restore_overlay(session, "1990:源条")
    assert r["source_preserved"] is True
    assert _tl_overlay(session, "1990:源条") is None


def test_source_as_latest_absorb_and_no_source(session):
    _seed_source(session, year=1990, title="A", note="源备注")
    create_overlay(session, event_year=1990, title="A", note="改过")
    r = source_as_latest(session, "1990:A")
    assert r["status"] == "synced" and "note" in r["synced_fields"]
    o = session.execute(select(UserDataOverlay).where(
        UserDataOverlay.key == "1990:A")).scalar_one()
    assert o.payload["note"] == "源备注"
    # 无源 → no-op
    create_overlay(session, event_year=2020, title="全新", note="x")
    assert source_as_latest(session, "2020:全新")["status"] == "no_source"


def test_system_row_isolation(session):
    """issue #86：系统 overlay 行(source_file=None) 不被 list/diff/restore/source-as-latest 触碰。"""
    _seed_system(session, year=1990, title="投资赎回")
    _seed_source(session, year=1990, title="源条")
    # 一个用户覆盖行 + 一个系统行（同 (year,title) 也无妨）
    create_overlay(session, event_year=1990, title="源条", note="改")
    # list_user_overlays 只含用户行
    assert [o.key for o in list_user_overlays(session)] == ["1990:源条"]
    # diff 只含用户行
    assert all(not d["key"].startswith("1990:投资赎回") for d in diff_overlay(session))
    # delete 系统行所在 key 是 no-op（系统行不在 user_data_overlay）
    sys_key = make_key(1990, "投资赎回")
    r = delete_overlay(session, sys_key)
    assert r["deleted"] == 0                                # 不删系统 timeline 行
    still = session.execute(select(TimelineEvent).where(
        TimelineEvent.overlay.is_(True), TimelineEvent.title == "投资赎回")).scalars().one()
    assert still.source_file is None                        # 系统行未被删除
    # source_as_latest 对系统行为 no_source（无源、无 user 行）
    assert source_as_latest(session, sys_key)["status"] == "no_source"