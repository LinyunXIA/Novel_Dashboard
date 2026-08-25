"""F-P2-06 import_all force_files 机制单测（DESIGN §11 采纳新版本靠它强制重导入）。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.ingest.main import _skip_by_state
from app.model import SourceFileVersion
from app.ingest.parse import ParseResult


@pytest.fixture
def db():
    from sqlalchemy import BigInteger, Integer
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    s = S()
    yield s
    s.close()
    engine.dispose()


def _rec(rel="a.md"):
    return ParseResult(file=rel, category="bank", records=[], warnings=[], error=None)


def test_skip_by_state_force_overrides(db, tmp_path):
    # 无版本 → new → 不跳过；但先造一个 unchanged 场景：
    db.add(SourceFileVersion(file_path="a.md", version=1, content="same\n",
                             captured_at=None, is_current=True))
    db.flush()
    (tmp_path / "a.md").write_text("same\n", encoding="utf-8")   # 磁盘与版本一致 → unchanged
    r = _rec()
    logs = []
    # 无 force：unchanged → 跳过
    assert _skip_by_state(db, r, tmp_path, logs.append) is True
    # force：命中文件 → 不跳过
    assert _skip_by_state(db, r, tmp_path, logs.append, force_files={"a.md"}) is False
    # force 不命中别的文件 → 仍跳过
    assert _skip_by_state(db, r, tmp_path, logs.append, force_files={"b.md"}) is True