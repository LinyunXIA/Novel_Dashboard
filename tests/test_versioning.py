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