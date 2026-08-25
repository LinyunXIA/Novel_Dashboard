"""issue #119 回归：date_rule 闭环——API 登记 → normalize 消费 → 超规则解析复用。"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine, Integer, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.app import app
from app.api.deps import get_db
from app.ingest.normalize import apply_user_date_rules, load_date_rules, parse_date_cell
from app.db import Base
from app.model import DateRule


@pytest.fixture
def client():
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def _override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c, Session
    app.dependency_overrides.clear()
    engine.dispose()


class TestNormalizeRules:
    def test_rule_resolves_out_of_spec_cell(self):
        load_date_rules([(1, "三伏", "07-15")])
        try:
            assert parse_date_cell("1990年三伏") == (date(1990, 7, 15), "date_rule:1")
        finally:
            load_date_rules([])

    def test_defaults_still_win_when_matched_earlier(self):
        """默认规则可解析的 cell 不走用户规则（规则只在失配后尝试）。"""
        load_date_rules([(1, r"\d{4}", "01-01")])
        try:
            assert parse_date_cell("1992") == (date(1992, 12, 30), "year")
        finally:
            load_date_rules([])

    def test_bad_regex_skipped_on_load(self):
        assert load_date_rules([(1, "([", "07-15"), (2, "三伏", "07-15"),
                                (3, "x", "bad")]) == 1

    def test_apply_without_year_prefix_returns_none(self):
        load_date_rules([(1, "三伏", "07-15")])
        try:
            assert apply_user_date_rules("三伏") is None
        finally:
            load_date_rules([])


class TestDateRuleApi:
    def test_crud_roundtrip_and_consumption(self, client):
        c, Session = client
        r = c.post("/api/v1/date-rules",
                   json={"pattern": "约\\d{4}年中", "resolve": "06-30", "note": "年中口径"})
        assert r.status_code == 201
        rid = r.json()["id"]

        lst = c.get("/api/v1/date-rules").json()
        assert lst["total"] == 1 and lst["items"][0]["pattern"].startswith("约")

        # 消费侧：模拟 ingest 启动装载 → parse_date_cell 命中
        s = Session()
        rows = s.execute(select(DateRule.id, DateRule.pattern, DateRule.resolve)).all()
        load_date_rules(rows)
        s.close()
        try:
            assert parse_date_cell("约1992年中盘点") == (date(1992, 6, 30), f"date_rule:{rid}")
        finally:
            load_date_rules([])

        assert c.put(f"/api/v1/date-rules/{rid}",
                     json={"pattern": "约\\d{4}年末", "resolve": "12-31"}).status_code == 200
        assert c.delete(f"/api/v1/date-rules/{rid}").status_code == 204
        assert c.get("/api/v1/date-rules").json()["total"] == 0

    def test_validation_errors(self, client):
        c, _ = client
        assert c.post("/api/v1/date-rules",
                      json={"pattern": "([", "resolve": "07-15"}).status_code == 422
        assert c.post("/api/v1/date-rules",
                      json={"pattern": "ok", "resolve": "7月15"}).status_code == 422
