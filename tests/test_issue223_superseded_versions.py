"""issue #223：SKIP_SUPERSEDED 文件的 source_file_version 自动失活。

整合取代（#211/#214/#218/#220）后旧文件从 Design_Folder 删除，但历史版本记录仍
is_current=True，diff/版本屏会把已删文件一直挂为「当前版本」。import_all 做版本
对账：detect(file_path) 命中 SKIP_SUPERSEDED 护栏 → is_current 置 false。
"""
from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, Integer, create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.ingest.main import import_all
from app.ingest.parse import IngestReport
from app.model import SourceFileVersion

OLD = "基准/收益表/惠民租房.md"              # #211：SKIP_SUPERSEDED 护栏
OLD_RT = "基准/收益表/1947-2025 欧洲R1-R5投资风险分级收益测算表.md"  # #214
KEEP = "人物/主角.md"                        # 现行文件，不得失活


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


def _add_version(s, path: str, current: bool = True):
    s.add(SourceFileVersion(file_path=path, version=1, content="x", is_current=current))


def _is_current(s, path: str) -> bool:
    return s.execute(
        select(SourceFileVersion.is_current).where(SourceFileVersion.file_path == path)
    ).scalar_one()


def test_superseded_versions_deactivated(session, monkeypatch, tmp_path):
    monkeypatch.setattr("app.ingest.main.run_ingest", lambda d: IngestReport())
    _add_version(session, OLD)
    _add_version(session, OLD_RT)
    _add_version(session, KEEP)
    session.flush()

    logs: list[str] = []
    import_all(session, tmp_path, log=logs.append)
    session.commit()

    assert _is_current(session, OLD) is False
    assert _is_current(session, OLD_RT) is False
    assert _is_current(session, KEEP) is True          # 现行文件不受影响
    assert any("SKIP_SUPERSEDED" in m and OLD in m for m in logs)


def test_already_inactive_untouched_and_idempotent(session, monkeypatch, tmp_path):
    """已失活记录不重复处理；二跑无新增失活、无日志噪音。"""
    monkeypatch.setattr("app.ingest.main.run_ingest", lambda d: IngestReport())
    _add_version(session, OLD, current=False)
    _add_version(session, KEEP)
    session.flush()

    logs1: list[str] = []
    import_all(session, tmp_path, log=logs1.append)
    assert _is_current(session, KEEP) is True
    n_log1 = len([m for m in logs1 if "SKIP_SUPERSEDED" in m])

    logs2: list[str] = []
    import_all(session, tmp_path, log=logs2.append)
    n_log2 = len([m for m in logs2 if "SKIP_SUPERSEDED" in m])
    assert n_log1 == 0 and n_log2 == 0                # 无 current 记录可失活
