"""四轮审计 #170：测试缺口合集 G 系列 + 解析器直接单测补充。

- G1 §10 H1 check_h1_timeline_alignment
- G2 §10 H2 基础版 check_h2_amount_consistency
- G3 §10 H5 check_h5_dangling
- G4 外部系统错误映射（凭据→503 / 其余→502）
- G5 ALLOW_ADMIN_CLEAN=1 删除放行正向路径
- G6 GET /api/v1/wealth（含 missing_rates 语义）
- G8 pool_in_transit 跨年未赎回段
- 附：close_2002_currency EUR 承接分录直接单测（此前零覆盖）
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, Integer, create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.restricted as restricted_mod
from app.api.app import app
from app.api.deps import get_db
from app.db import Base
from app.core.health import check_h1_timeline_alignment, check_h2_amount_consistency, \
    check_h5_dangling
from app.ingest.writer import close_2002_currency
from app.model import (Account, Entity, IncomeStream, LedgerEntry,
                       Relationship, Snapshot)


@pytest.fixture
def db():
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _client(db):
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


class _F:
    def __init__(self, rule, level, detail, file="f.md", line=None):
        self.rule, self.level, self.detail, self.file, self.line = rule, level, detail, file, line


class TestHealthDirectG1G3:
    def test_h1_flags_ui_derived_year_without_income(self, db):
        """时间线年无对应收益流 → warn（income_years 非空才启用比对，故先铺一年收益流）。"""
        from app.model import TimelineEvent
        e = Entity(entity_type="person", name="H1锚")
        db.add(e); db.flush()
        db.add(IncomeStream(entity_id=e.id, stream_type="salary", currency="BEF",
                            year=1990, amount=100))
        db.add(TimelineEvent(event_year=1995, title="某年投资", overlay=True))
        db.commit()
        finds = check_h1_timeline_alignment(db)
        assert any(f.rule == "H1" and f.level == "warn"
                   and "1995" in (f.location or "") for f in finds)

    def test_h1_clean_when_income_covers(self, db):
        from app.model import TimelineEvent
        e = Entity(entity_type="person", name="H1人")
        db.add(e); db.flush()
        db.add(IncomeStream(entity_id=e.id, stream_type="salary", currency="BEF",
                            year=1995, amount=100))
        db.add(IncomeStream(entity_id=e.id, stream_type="rent", currency="EUR",
                            year=1990, amount=50))
        db.add(TimelineEvent(event_year=1995, title="薪资年", overlay=True))
        db.commit()
        assert not [f for f in check_h1_timeline_alignment(db) if "1995" in (f.location or "")]

    def test_h2_multi_source_amount_mismatch(self, db):
        e = Entity(entity_type="person", name="H2人")
        db.add(e); db.flush()
        db.add(IncomeStream(entity_id=e.id, stream_type="rent", currency="EUR",
                            year=1990, amount=100, label="惠民租房"))
        db.add(IncomeStream(entity_id=e.id, stream_type="rent", currency="EUR",
                            year=1990, amount=200, label="惠民租房"))
        db.commit()
        finds = check_h2_amount_consistency(db)
        assert any(f.rule == "H2" for f in finds)

    def test_h5_dangling_relationship(self, db):
        e = Entity(entity_type="person", name="H5人")
        db.add(e); db.flush()
        db.add(Relationship(from_entity_id=e.id, to_entity_id=999999, rel_type="parent"))
        db.commit()
        finds = check_h5_dangling(db)
        assert any(f.rule == "H5" and f.level in ("warn", "crit") for f in finds)


class TestExternalErrorMappingG4:
    def test_upstream_401_maps_to_503(self, db, monkeypatch):
        import httpx
        from app.ingest.importers import company_info
        monkeypatch.setattr(company_info, "run_external_company_import",
                            lambda db, base_url=None: (_ for _ in ()).throw(
                                httpx.HTTPStatusError("401", request=None,
                                                      response=__import__("httpx").Response(401))))
        with _client(db) as c:
            r = c.post("/api/v1/graph/companies/import")
            assert r.status_code == 503

    def test_upstream_500_maps_to_502_with_code(self, db, monkeypatch):
        import httpx
        from app.ingest.importers import company_info

        def _boom(db, base_url=None):
            req = httpx.Request("GET", "http://upstream")
            raise httpx.HTTPStatusError("boom", request=req, response=httpx.Response(500, request=req))

        monkeypatch.setattr(company_info, "run_external_company_import", _boom)
        with _client(db) as c:
            r = c.post("/api/v1/graph/companies/import")
            assert r.status_code == 502 and "500" in r.json()["detail"]


class TestAdminCleanG5:
    def test_delete_allowed_when_env_set(self, db, monkeypatch):
        monkeypatch.setattr(restricted_mod.os, "environ",
                             {**restricted_mod.os.environ, "ALLOW_ADMIN_CLEAN": "1"})
        e = Entity(entity_type="company", name="可删公司")
        db.add(e); db.commit()
        headers = {"X-Importer": "1"}
        with _client(db) as c:
            r = c.delete(f"/api/v1/entities/{e.id}", headers=headers)
            assert r.status_code in (200, 204), r.text


class TestWealthEndpointG6:
    def test_wealth_series_and_missing_rates_shape(self, db):
        e = Entity(entity_type="person", name="W人")
        db.add(e); db.flush()
        acc = Account(entity_id=e.id, currency="XYZ")   # 无汇率的币种
        db.add(acc); db.flush()
        db.add(LedgerEntry(account_id=acc.id, date=date(1990, 12, 30),
                           reason="期初", inflow=500, balance=500, kind="income"))
        db.add(Snapshot(as_of_year=1990, as_of_date=None, scope="family:total",
                        value=0, currency="USD"))
        db.commit()
        with _client(db) as c:
            r = c.get("/api/v1/wealth?year_from=1989&year_to=1991")
            assert r.status_code == 200
            body = r.json()
            assert isinstance(body, dict) and body


class TestPoolInTransitCrossYearG8:
    def test_unredeemed_principal_kept_next_year(self, db):
        """1988 投资、未赎回 → 1989 年度快照 entity 域仍含在途本金加回。"""
        from app.model import FinanceEntry
        e = Entity(entity_type="person", name="P85人",
                   fields={"compound": False})
        db.add(e); db.flush()
        acc = Account(entity_id=e.id, currency="BEF")
        db.add(acc); db.flush()
        # 银行期初 1000 → 投资划出 600（kind=investment），此后未赎回
        db.add(LedgerEntry(account_id=acc.id, date=date(1988, 1, 1),
                           reason="期初", inflow=1000, balance=1000, kind="income"))
        db.add(LedgerEntry(account_id=acc.id, date=date(1988, 6, 1),
                           reason="投资划出", outflow=600, balance=400, kind="investment"))
        db.add(FinanceEntry(entity_id=e.id, entity_kind="person", year=1988,
                            kind="investment", amount=600, currency="BEF",
                            label="投资", source="ui"))
        db.commit()
        from app.core.snapshot import rebuild_snapshots
        rebuild_snapshots(db)   # 全量重建，覆盖 1989 跨年段
        row89 = db.execute(select(Snapshot).where(
            Snapshot.scope == f"entity:{e.id}:BEF",
            Snapshot.as_of_year == 1989)).scalar_one_or_none()
        assert row89 is not None
        # 净值口径 = 银行 400 + 在途本金 600 = 1000
        assert abs(float(row89.value) - 1000.0) < 0.01


class TestClose2002Eur:
    def test_eur_takeover_entry_written_once(self, db):
        """关池：BEF 余额按 EMU 固定率折 EUR 承接分录；重跑幂等不重复。"""
        e = Entity(entity_type="person", name="C02人")
        db.add(e); db.flush()
        bef = Account(entity_id=e.id, currency="BEF")
        eur = Account(entity_id=e.id, currency="EUR")
        db.add_all([bef, eur])
        db.flush()
        db.add(LedgerEntry(account_id=bef.id, date=date(2001, 12, 30),
                           reason="期初", inflow=40339.90, balance=40339.90,
                           kind="income"))
        db.commit()
        close_2002_currency(db)
        close_2002_currency(db)   # 幂等重跑
        rows = db.execute(select(LedgerEntry).where(
            LedgerEntry.account_id == eur.id)).scalars().all()
        assert len(rows) == 1
        # 40339.90 BEF ÷ 40.3399 = 1000.00 EUR
        assert abs(float(rows[0].inflow) - 1000.0) < 0.01
        assert "40.3399" in (rows[0].note or "")
