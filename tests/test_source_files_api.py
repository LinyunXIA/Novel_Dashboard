"""F-P2-06 source-files API 通路单测（DESIGN §11）。"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.app import app
from app.api.deps import get_db
from app.api import source_files as sf
from app.core import versioning
from app.db import Base
from app.model import SourceFileVersion


@pytest.fixture
def db():
    from sqlalchemy import BigInteger, Integer
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


def _seed(db, rel="a.md", content="line1\nline2\n", version=1, current=True):
    db.add(SourceFileVersion(file_path=rel, version=version, content=content,
                             captured_at=date(2026, 1, 1), is_current=current))
    db.flush()


class TestSourceFilesApi:
    def test_list_and_diff(self, db, tmp_path, monkeypatch):
        _seed(db)
        (tmp_path / "a.md").write_text("line1\nCHANGED\n", encoding="utf-8")
        cfg = SimpleNamespace(source_dir=tmp_path)
        monkeypatch.setattr(sf, "get_config", lambda: cfg)
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as c:
                lst = c.get("/api/v1/source-files").json()["items"]
                cur = next(x for x in lst if x["file"] == "a.md")
                assert cur["status"] == "changed"
                vid = cur["current_version"]
                d = c.get(f"/api/v1/source-files/{vid}/diff").json()
                assert d["changed"] is True and "+CHANGED" in d["diff_str"]
                vs = c.get(f"/api/v1/source-files/{vid}/versions").json()
                assert vs["file"] == "a.md" and len(vs["versions"]) == 1
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_adopt_endpoint(self, db, tmp_path, monkeypatch):
        _seed(db, content="OLD\n", version=1, current=True)
        (tmp_path / "a.md").write_text("NEW\n", encoding="utf-8")
        cfg = SimpleNamespace(source_dir=tmp_path)
        monkeypatch.setattr(sf, "get_config", lambda: cfg)

        def fake(db2, source_dir, log=None, force_files=None):
            old = db2.execute(select(SourceFileVersion).where(
                SourceFileVersion.version == 1)).scalar_one()
            old.is_current = False
            db2.add(SourceFileVersion(file_path="a.md", version=2, content="NEW\n",
                                      captured_at=date(2026, 1, 2), is_current=True))
            db2.flush()
        monkeypatch.setattr(versioning, "import_all", fake)
        app.dependency_overrides[get_db] = lambda: db
        try:
            vid = db.execute(select(SourceFileVersion).where(
                SourceFileVersion.version == 1)).scalar_one().id
            with TestClient(app) as c:
                r = c.post(f"/api/v1/source-files/{vid}/versions")
                assert r.status_code == 200 and r.json()["status"] == "adopted"
                v2 = db.execute(select(SourceFileVersion).where(
                    SourceFileVersion.version == 2)).scalar_one()
                assert v2.is_current is True and (tmp_path / "a.md").read_text() == "NEW\n"
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_restore_endpoint(self, db, tmp_path, monkeypatch):
        _seed(db, content="OLD v1\n", version=1, current=False)
        _seed(db, content="NEW v2\n", version=2, current=True)
        cfg = SimpleNamespace(source_dir=tmp_path)
        (tmp_path / "a.md").write_text("NEW v2\n", encoding="utf-8")
        monkeypatch.setattr(sf, "get_config", lambda: cfg)
        app.dependency_overrides[get_db] = lambda: db
        try:
            v1 = db.execute(select(SourceFileVersion).where(
                SourceFileVersion.version == 1)).scalar_one()
            v2 = db.execute(select(SourceFileVersion).where(
                SourceFileVersion.version == 2)).scalar_one()
            with TestClient(app) as c:
                r = c.post(f"/api/v1/source-files/{v2.id}/versions/{v1.id}/restore")
                assert r.status_code == 200 and r.json()["status"] == "restored"
                assert (tmp_path / "a.md").read_text(encoding="utf-8") == "OLD v1\n"
                v1b = db.execute(select(SourceFileVersion).where(
                    SourceFileVersion.version == 1)).scalar_one()
                assert v1b.is_current is True
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_restore_bad_version_404(self, db, tmp_path, monkeypatch):
        _seed(db)
        cfg = SimpleNamespace(source_dir=tmp_path)
        monkeypatch.setattr(sf, "get_config", lambda: cfg)
        app.dependency_overrides[get_db] = lambda: db
        try:
            vid = db.execute(select(SourceFileVersion)).scalar_one().id
            with TestClient(app) as c:
                assert c.post(f"/api/v1/source-files/{vid}/versions/999999/restore").status_code == 404
        finally:
            app.dependency_overrides.pop(get_db, None)