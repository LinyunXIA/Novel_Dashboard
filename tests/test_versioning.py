"""F-P2-06 文件版本/diff/回退服务层单测（DESIGN §11）。"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core import versioning
from app.core.versioning import (adopt_current, diff_texts, file_diff, list_tracked,
                                 restore_version)
from app.db import Base
from app.model import Notification, SourceFileVersion


@pytest.fixture
def db():
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


@pytest.fixture
def src(tmp_path):
    """临时 source_dir + fake config。"""
    cfg = SimpleNamespace(source_dir=tmp_path)
    (tmp_path / "a.md").write_text("line1\nline2\n", encoding="utf-8")
    return cfg, tmp_path


def _seed_version(db, rel="a.md", content="line1\nline2\n", version=1, current=True):
    db.add(SourceFileVersion(file_path=rel, version=version, content=content,
                             captured_at=date(2026, 1, 1), is_current=current))
    db.flush()
    return db.execute(select(SourceFileVersion).where(
        SourceFileVersion.file_path == rel, SourceFileVersion.version == version)).scalar_one()


def test_diff_texts(db):
    d = diff_texts("a\nb\n", "a\nc\n")
    assert "-b" in d and "+c" in d and "a" in d


def test_list_tracked_statuses(db, src):
    cfg, _ = src
    _seed_version(db)                              # is_current content == disk line1/line2
    res = {x["file"]: x for x in list_tracked(db, cfg)}
    assert res["a.md"]["status"] == "unchanged"
    # 改磁盘 → changed
    (cfg.source_dir / "a.md").write_text("line1\nCHANGED\n", encoding="utf-8")
    res = {x["file"]: x for x in list_tracked(db, cfg)}
    assert res["a.md"]["status"] == "changed"
    # 无版本文件 → new
    (cfg.source_dir / "b.md").write_text("x", encoding="utf-8")
    res = {x["file"]: x for x in list_tracked(db, cfg)}
    assert res["b.md"]["status"] == "new"
    # 新文件无磁盘 → 不算（源目录才跟踪）；只有 a.md 有版本，b 是 new


def test_file_diff(db, src):
    cfg, _ = src
    _seed_version(db)
    (cfg.source_dir / "a.md").write_text("line1\nCHANGED\n", encoding="utf-8")
    r = file_diff(db, cfg, "a.md")
    assert r["changed"] is True and "+CHANGED" in r["diff_str"] and r["lines_add"] >= 1


def test_restore_writes_disk_and_switches_is_current(db, src):
    cfg, tmp = src
    _seed_version(db, content="OLD v1\n", version=1, current=False)
    _seed_version(db, content="NEW v2\n", version=2, current=True)
    # 磁盘当前 = v2
    (cfg.source_dir / "a.md").write_text("NEW v2\n", encoding="utf-8")
    old = db.execute(select(SourceFileVersion).where(
        SourceFileVersion.version == 1)).scalar_one()
    r = restore_version(db, cfg, "a.md", old.id)
    assert r["status"] == "restored" and r["version"] == 1
    assert (cfg.source_dir / "a.md").read_text(encoding="utf-8") == "OLD v1\n"   # 磁盘复原
    v1 = db.execute(select(SourceFileVersion).where(
        SourceFileVersion.version == 1)).scalar_one()
    v2 = db.execute(select(SourceFileVersion).where(
        SourceFileVersion.version == 2)).scalar_one()
    assert v1.is_current is True and v2.is_current is False
    _ = tmp
    # notification 写入
    n = db.execute(select(Notification).where(Notification.kind == "file-updated")).scalars().one()
    assert n.payload["status"] in ("restored", "adopted")


def test_restore_path_traversal_rejected(db, tmp_path):
    cfg = SimpleNamespace(source_dir=tmp_path)
    # 版本 file_path 本身是越权 rel（../escape.md）→ restore 必须被 _safe_target 拒绝
    v = _seed_version(db, rel="../escape.md", content="x")
    with pytest.raises(ValueError):
        restore_version(db, cfg, "../escape.md", v.id)
    assert not (tmp_path.parent / "escape.md").exists()   # 未写越权


def test_restore_blocked_when_disk_diverged_from_current(db, src):
    """issue #139：磁盘内容已偏离当前生效版 → RestoreConflict，绝不无提示覆盖。"""
    cfg, tmp = src
    _seed_version(db, content="OLD v1\n", version=1, current=False)
    _seed_version(db, content="NEW v2\n", version=2, current=True)
    (tmp / "a.md").write_text("NEW v2\n", encoding="utf-8")
    old = db.execute(select(SourceFileVersion).where(
        SourceFileVersion.version == 1)).scalar_one()
    # 数据调整员在决策期间又手改了磁盘
    (tmp / "a.md").write_text("THIRD-PARTY EDIT\n", encoding="utf-8")
    with pytest.raises(versioning.RestoreConflict):
        restore_version(db, cfg, "a.md", old.id)
    assert (tmp / "a.md").read_text(encoding="utf-8") == "THIRD-PARTY EDIT\n"   # 未被覆盖
    v1 = db.execute(select(SourceFileVersion).where(
        SourceFileVersion.version == 1)).scalar_one()
    assert v1.is_current is False                        # 版本状态未变


def test_restore_allowed_when_disk_matches_current(db, src):
    """issue #139：磁盘与当前版一致（正常回退场景）→ 照常复原。"""
    cfg, tmp = src
    _seed_version(db, content="OLD v1\n", version=1, current=False)
    _seed_version(db, content="NEW v2\n", version=2, current=True)
    (tmp / "a.md").write_text("NEW v2\n", encoding="utf-8")
    old = db.execute(select(SourceFileVersion).where(
        SourceFileVersion.version == 1)).scalar_one()
    r = restore_version(db, cfg, "a.md", old.id)
    assert r["status"] == "restored"
    assert (tmp / "a.md").read_text(encoding="utf-8") == "OLD v1\n"


def test_adopt_wires_force_import_and_notifies(db, src, monkeypatch):
    cfg, _ = src
    _seed_version(db, content="OLD\n", version=1, current=True)
    (cfg.source_dir / "a.md").write_text("NEW content\n", encoding="utf-8")
    called = {}

    def fake_import_all(session, source_dir, log=None, force_files=None):
        called["force_files"] = force_files
        # 模拟重导入后记录新版本为 is_current
        session.add(SourceFileVersion(file_path="a.md", version=2, content="NEW content\n",
                                      captured_at=date(2026, 1, 2), is_current=True))
        old = session.execute(select(SourceFileVersion).where(
            SourceFileVersion.version == 1)).scalar_one()
        old.is_current = False
        session.flush()

    monkeypatch.setattr(versioning, "import_all", fake_import_all)
    r = adopt_current(db, cfg, "a.md")
    assert r["status"] == "adopted" and r["version"] == 2
    assert called["force_files"] == {"a.md"}
    v2 = db.execute(select(SourceFileVersion).where(
        SourceFileVersion.version == 2)).scalar_one()
    assert v2.is_current is True

# ---- issue #226：已整合取代（SKIP_SUPERSEDED）文件的展示与回退防护 ----

SUPERSEDED = "基准/收益表/惠民租房.md"   # detect=SKIP_SUPERSEDED（#211 旧收益文件）


def test_list_tracked_marks_superseded_when_no_current_and_no_disk(db, src):
    """无 is_current 版本且磁盘已删 → superseded、current_version=None；现行文件不受影响。"""
    cfg, _ = src
    _seed_version(db)                                              # a.md：现行，unchanged
    _seed_version(db, rel=SUPERSEDED, content="OLD\n", version=1, current=False)
    res = {x["file"]: x for x in list_tracked(db, cfg)}
    assert res["a.md"]["status"] == "unchanged"
    assert res[SUPERSEDED]["status"] == "superseded"
    assert res[SUPERSEDED]["current_version"] is None


def test_list_tracked_disk_file_without_version_is_new_not_superseded(db, src):
    """磁盘有文件但无版本记录 → new（不被误判 superseded）。"""
    cfg, _ = src
    (cfg.source_dir / "fresh.md").write_text("x\n", encoding="utf-8")
    res = {x["file"]: x for x in list_tracked(db, cfg)}
    assert res["fresh.md"]["status"] == "new"


def test_restore_refused_for_superseded_file(db, src):
    """SKIP_SUPERSEDED 文件回退 → RestoreConflict；不写盘、is_current 不复活。"""
    cfg, tmp = src
    v1 = _seed_version(db, rel=SUPERSEDED, content="OLD\n", version=1, current=False)
    with pytest.raises(versioning.RestoreConflict):
        restore_version(db, cfg, SUPERSEDED, v1.id)
    assert not (cfg.source_dir / SUPERSEDED).exists()               # 旧文件未被写回
    row = db.execute(select(SourceFileVersion).where(
        SourceFileVersion.file_path == SUPERSEDED)).scalar_one()
    assert row.is_current is False
    _ = tmp


def test_restore_refused_when_no_current_version(db, src):
    """无 is_current 版本的普通文件（版本全失活+磁盘已删）→ 同样拒绝回退。"""
    cfg, _ = src
    v1 = _seed_version(db, rel="a.md", content="OLD\n", version=1, current=False)
    (cfg.source_dir / "a.md").unlink()                              # 磁盘删除
    with pytest.raises(versioning.RestoreConflict):
        restore_version(db, cfg, "a.md", v1.id)
    assert not (cfg.source_dir / "a.md").exists()


def test_adopt_refused_for_superseded_or_missing_file(db, src, monkeypatch):
    """采纳对 SKIP_SUPERSEDED → 409 语义；磁盘不存在 → 422 语义；均不触发 import_all。"""
    cfg, _ = src
    called = {"n": 0}

    def fake_import_all(*a, **k):
        called["n"] += 1

    monkeypatch.setattr(versioning, "import_all", fake_import_all)
    with pytest.raises(versioning.RestoreConflict):
        adopt_current(db, cfg, SUPERSEDED)
    (cfg.source_dir / "a.md").unlink()                              # 现行文件磁盘删除
    with pytest.raises(ValueError):
        adopt_current(db, cfg, "a.md")
    assert called["n"] == 0                                         # 拦截先于重导入
