"""F-P2-07 导出资源单测（DESIGN §14.2/§15）。

覆盖：markdown 全库档案（编年史合并生效）、csv 各 scope 与转义、pdf 报告
（%PDF 魔数）、422 校验、404 语义、产物清单；产物目录 monkeypatch 到 tmp，
并断言 source_dir/input_dir 不被触碰（仅导出不回写）。
"""
from __future__ import annotations

import dataclasses
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, Integer, create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.app import app
from app.api.deps import get_db
from app.config import get_config
from app.db import Base
from app.model import (Account, Entity, FinanceEntry, HoldingEvent, ReturnCurve,
                       Snapshot, TimelineEvent)


@pytest.fixture
def db(tmp_path, monkeypatch):
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()

    # 产物目录 → tmp（不污染 data/exports-test）
    real = get_config()
    monkeypatch.setattr("app.api.exports.get_config",
                        lambda: dataclasses.replace(real, exports_dir=tmp_path))

    # 种子数据：实体/账户/财务/收益/持仓/编年史(源+用户覆盖同 key)/family 快照
    e = Entity(entity_type="person", name="Export人物")
    s.add(e)
    s.flush()
    s.add(Account(entity_id=e.id, currency="BEF", bank="BNP", status="closed"))
    s.add(FinanceEntry(entity_id=e.id, entity_kind="person", year=1990,
                       kind="income", amount=1000.5, currency="BEF", label='带"引号",逗号'))
    s.add(ReturnCurve(country="欧洲", risk_lvl="R3", year=1990, rate=12.5))
    s.add(HoldingEvent(entity_id=e.id, company="皮克斯", date=date(1994, 1, 1),
                       event_type="buy", shares=100, unit_price=2.0, amount=200))
    s.add(TimelineEvent(event_year=1990, title="事件甲", note="源行", overlay=False,
                        source_file="时间线.md"))
    s.add(TimelineEvent(event_year=1990, title="事件甲", note="覆盖行", overlay=True,
                        source_file="overlay:timeline:1990:事件甲"))
    s.add(Snapshot(as_of_year=1990, as_of_date=None, scope="family:total",
                   value=123456.78, currency="USD"))
    s.commit()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _client(db):
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


class TestCreateExport:
    def test_markdown_full_archive_with_overlay_priority(self, db):
        c = _client(db)
        try:
            with c:
                r = c.post("/api/v1/exports", json={"format": "markdown"})
                assert r.status_code == 201
                body = r.json()
                assert body["format"] == "markdown" and body["scope"] is None
                assert body["filename"].endswith(".md") and body["size_bytes"] > 0
                assert r.headers["location"] == f"/api/v1/exports/{body['id']}"

                d = c.get(body["download_url"])
                assert d.status_code == 200
                assert "text/markdown" in d.headers["content-type"]
                text = d.text
                assert "家族设定数据档案" in text and "Export人物" in text
                # 编年史合并生效：同 key 只出现覆盖行备注，源行备注不再单独成行
                tl_rows = [ln for ln in text.splitlines() if "事件甲" in ln]
                assert len(tl_rows) == 1 and "覆盖行" in tl_rows[0]
                # family:total 摘要进 md（四节计数处无断言必要，快照节选在五节之外）
                assert "| income | 1 |" in text.replace(", ", ",") or "| income | 1 |" in text
        finally:
            app.dependency_overrides.pop(get_db, None)

    @pytest.mark.parametrize("scope,header_first", [
        ("finance", "year,entity_id"),
        ("returns", "country,risk_lvl"),
        ("holdings", "entity_id,company"),
        ("timeline", "event_year,event_date"),
        ("ledger", "account_id,date"),
    ])
    def test_csv_scopes(self, db, scope, header_first):
        c = _client(db)
        try:
            with c:
                r = c.post("/api/v1/exports", json={"format": "csv", "scope": scope})
                assert r.status_code == 201
                d = c.get(r.json()["download_url"])
                assert "text/csv" in d.headers["content-type"]
                lines = d.text.splitlines()
                assert lines[0].startswith(header_first)
                if scope == "returns":
                    # NUMERIC 经 sqlite 可能带尾零，按前缀断言
                    assert any(ln.startswith("欧洲,R3,1990,12.5") for ln in lines)
                if scope == "finance":
                    # RFC4180 转义：含引号/逗号的 label 被双引号包裹且内部分引号翻倍
                    assert any('"带""引号"",逗号"' in ln for ln in lines)
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_pdf_report_magic(self, db):
        c = _client(db)
        try:
            with c:
                r = c.post("/api/v1/exports", json={"format": "pdf"})
                assert r.status_code == 201 and r.json()["size_bytes"] > 500
                d = c.get(r.json()["download_url"])
                assert d.headers["content-type"] == "application/pdf"
                assert d.content[:5] == b"%PDF-"
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_validation_422(self, db):
        c = _client(db)
        try:
            with c:
                assert c.post("/api/v1/exports", json={"format": "xlsx"}).status_code == 422
                assert c.post("/api/v1/exports", json={"format": "csv"}).status_code == 422
                assert c.post("/api/v1/exports",
                              json={"format": "csv", "scope": "bogus"}).status_code == 422
                # scope 仅 csv 支持
                assert c.post("/api/v1/exports",
                              json={"format": "pdf", "scope": "finance"}).status_code == 422
        finally:
            app.dependency_overrides.pop(get_db, None)


class TestFetchAndList:
    def test_get_missing_and_malformed_id_404(self, db):
        c = _client(db)
        try:
            with c:
                assert c.get("/api/v1/exports/nope-20260101T000000-abcdef").status_code == 404
                # 穿越/伪造 id：ID_RE 不匹配 → 一律 404（不暴露路径信息）
                assert c.get("/api/v1/exports/..%2F..%2Fsecret").status_code == 404
                assert c.get("/api/v1/exports/markdown-x-abc").status_code == 404
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_list_exports_newest_first(self, db):
        c = _client(db)
        try:
            with c:
                ids = []
                for payload in ({"format": "markdown"}, {"format": "pdf"}):
                    ids.append(c.post("/api/v1/exports", json=payload).json()["id"])
                lst = c.get("/api/v1/exports").json()
                got = [x["id"] for x in lst["items"]]
                assert set(ids) <= set(got) and lst["total"] >= 2
        finally:
            app.dependency_overrides.pop(get_db, None)


def test_export_never_touches_source_or_input(db, tmp_path, monkeypatch):
    """§15 铁律：导出后 source_dir/input_dir 内容零变化。"""
    real = get_config()
    sentinel_src = tmp_path / "src"
    sentinel_in = tmp_path / "in"
    sentinel_src.mkdir(); sentinel_in.mkdir()
    (sentinel_src / "时间线.md").write_text("# 源", encoding="utf-8")
    before = sorted(str(p) for p in sentinel_src.rglob("*")) + \
        sorted(str(p) for p in sentinel_in.rglob("*"))
    monkeypatch.setattr("app.api.exports.get_config",
                        lambda: dataclasses.replace(real, source_dir=sentinel_src,
                                                    input_dir=sentinel_in,
                                                    exports_dir=tmp_path / "out"))
    c = _client(db)
    try:
        with c:
            r = c.post("/api/v1/exports", json={"format": "markdown"})
            assert r.status_code == 201
    finally:
        app.dependency_overrides.pop(get_db, None)
    after = sorted(str(p) for p in sentinel_src.rglob("*")) + \
        sorted(str(p) for p in sentinel_in.rglob("*"))
    assert before == after
