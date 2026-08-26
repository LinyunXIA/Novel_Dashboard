"""用工成本公式 + 落账单测（API② · F-P1-10）。

覆盖：内部全职基准年薪/Level 调整、逐年 CPI 增幅（负不涨）、外包基准系数、
日本 3 月奖金、在岗月折算、按公司聚合写 finance_entry、加薪规则 payload。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.app import app
from app.api.deps import get_db
from app.api.labor_cost import compute as labor_compute
from app.core import labor_cost as L
from app.db import Base
from app.ingest.importers import positions
from app.model import Entity, FinanceEntry
from app.model.labor import LaborCpiGrowth, LaborTaxBenchmark, LaborWageBenchmark


@pytest.fixture
def db():
    from sqlalchemy import BigInteger, Integer
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    s = S()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _seed_be(db):
    """比利时基准：工资(1982/2000)、CPI(1982-83)、税率(1982/2000)。"""
    db.add_all([
        LaborWageBenchmark(region="比利时", year=1982, currency="BEF",
                           investment_fin_salary=1010595.0, avg_salary=673730.0,
                           cpi_index=35.05, cpi_base_year=2013),
        LaborWageBenchmark(region="比利时", year=2000, currency="BEF",
                           investment_fin_salary=2190000.0, avg_salary=1460000.0),
        LaborCpiGrowth(region="比利时", year=1983, wage_growth_pct=8.1, cpi_pct=7.66),
        LaborTaxBenchmark(office="比利时", year=1982, formula="onss",
                          params={"onss_pct": 31.5, "wc_pct": 0.32}),
    ])
    db.commit()


def _pos(**over):
    d = {"work_location": "布鲁塞尔", "level": "B8b", "opening_date": "1982-01-01",
         "closing_date": None, "position_type": "Employee",
         "position_name": "Executive Assistant",
         "legal_category": "法律强制·内部全职不可外包",
         "company_id": 5, "company_name": "Peeters Luxembourg S.à r.l."}
    d.update(over)
    return d


def test_internal_salary_table_30pct(db):
    _seed_be(db)
    # B8b 按级别表 = 30% → ×1.3（注意：用户示例文字写 B8b→1.2，与级别表 30% 冲突，见待确认清单）
    assert L.compute_annual_salary(db, _pos(), 1982) == pytest.approx(1010595 * 1.3)
    # B8a(20%) 等比验证 ×1.2
    s = L.compute_annual_salary(db, _pos(level="B8a"), 1982)
    assert s == pytest.approx(1010595 * 1.2)


def test_cpi_increase_and_negative_no_raise(db):
    _seed_be(db)
    assert L.compute_annual_salary(db, _pos(), 1983) == pytest.approx(1010595 * 1.3 * 1.081)
    # 负增幅 → 不涨（插入 1984 负增长，1982→1984 应与 1982 保持一致增长率链：仅1983涨）
    db.add(LaborCpiGrowth(region="比利时", year=1984, wage_growth_pct=-3.0, cpi_pct=1.0))
    db.commit()
    assert L.compute_annual_salary(db, _pos(), 1984) == pytest.approx(1010595 * 1.3 * 1.081)


def test_outsourced_uses_avg_x_factor(db):
    _seed_be(db)
    p = _pos(position_type="Outsourced External - External Employee", level=None,
             opening_date="2000-01-01", legal_category="法律强制·允许第三方外包",
             work_location="布鲁塞尔")
    # 当年 avg(1460000) × 1.2
    assert L.compute_annual_salary(db, p, 2000) == pytest.approx(1460000 * 1.2)


def test_locate_prefers_work_location_then_country(db):
    _seed_be(db)
    # country_or_region 兜底：work_location 空 → 用 "Country·卢森堡"
    p = _pos(work_location="", country_or_region="Country·卢森堡")
    # 卢森堡无税率基准（_seed只比利时），但应先定位到 region=卢森堡
    c = L._locate(p)
    assert c is not None and c[0] == "卢森堡"
    # work_location 优先：即便 country 写布鲁塞尔，仍以 work_location 为准
    p2 = _pos(work_location="布鲁塞尔", country_or_region="Country·卢森堡")
    assert L._locate(p2)[0] == "比利时"
    # 比利时成本可算
    r = L.compute_position_cost(db, p2, 1982)
    assert r is not None and r["salary"] == pytest.approx(1010595 * 1.3)


def test_japan_3_month_bonus(db):
    db.add(LaborWageBenchmark(region="日本东京", year=2002, currency="JPY",
                              investment_fin_salary=5790000.0, avg_salary=4480000.0))
    db.add(LaborTaxBenchmark(office="日本东京", year=2002, formula="jp",
                             params={"kosei_pct": 8.675, "kenpo_pct": 4.25, "kaigo_pct": 0.41,
                                     "koyo_pct": 0.85, "rosa_pct": 0.25,
                                     "kosei_cap": 590000, "kenpo_cap": 980000}))
    db.commit()
    p = _pos(work_location="东京", level="B6", opening_date="2002-01-01")
    r = L.compute_position_cost(db, p, 2002)
    # 日本基准×1.05(B6) = 5790000*1.05；奖金 = salary/12*3
    assert r["bonus"] == pytest.approx((5790000 * 1.05) / 12 * 3)


def test_overlap_months(db):
    assert L._overlap_months("2001-06-15", None, 2002) == 12
    assert L._overlap_months("2002-06-15", None, 2002) == 7      # 6月~12月
    assert L._overlap_months("2000-01-01", "2002-03-31", 2002) == 3  # 1~3月
    assert L._overlap_months(None, None, 2002) == 0
    assert L._overlap_months("2003-01-01", None, 2002) == 0      # opening 在年后 → 不计


def test_aggregate_writes_finance_entry(db):
    _seed_be(db)
    db.add(Entity(entity_type="company", name="Peeters Luxembourg S.à r.l."))
    db.commit()
    costs = [L.compute_position_cost(db, _pos(), 1982)]
    agg = positions.aggregate_to_finance(db, costs, 1982)
    db.commit()
    assert agg["companies"][0]["positions"] == 1
    ent = db.execute(select(Entity).where(Entity.name == "Peeters Luxembourg S.à r.l.")).scalar_one()
    fe = db.execute(select(FinanceEntry).where(FinanceEntry.entity_id == ent.id)).scalar_one()
    assert fe.kind == "expense" and fe.entity_kind == "company"
    assert fe.currency == "BEF" and str(fe.label).startswith("用工成本·")


def test_rules_payload():
    r = L.rules_payload()
    assert r["level_adjust_pct"]["M11b"] == 5 and r["level_adjust_pct"]["M11a"] == 100
    assert r["bonus_months_japan"] == 3 and r["bonus_months_default"] == 2
    assert r["promotion_step_pct"] == 5


class TestEndpoint:
    def test_compute_route_with_stubbed_runner(self, db):
        """POST /compute：monkeypatch run_labor_cost 成功 → 200，且每公司落账。"""
        _seed_be(db)
        db.add(Entity(entity_type="company", name="Peeters Luxembourg S.à r.l."))
        db.commit()
        monkeypatch = pytest.MonkeyPatch()
        import app.api.labor_cost as api_mod
        monkeypatch.setattr(api_mod, "run_labor_cost",
                            lambda s, year, cids=None: _fake_runner(s, year))
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as c:
                r = c.post("/api/v1/labor-cost/compute", json={"year": 1982})
                assert r.status_code == 200, r.text
                assert r.json()["positions_fetched"] == 1
                # 落账后 results 可见
                res = c.get("/api/v1/labor-cost/results?year=1982").json()["items"]
                assert res and res[0]["company_name"] == "Peeters Luxembourg S.à r.l."
        finally:
            monkeypatch.undo()
            app.dependency_overrides.pop(get_db, None)

    def test_rules_and_results_routes(self, db):
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as c:
                r = c.get("/api/v1/labor-cost/rules").json()
                assert r["level_adjust_pct"]["M11b"] == 5
                rr = c.get("/api/v1/labor-cost/results?year=1982")
                assert rr.status_code == 200 and "items" in rr.json()   # 七轮审计 #183
        finally:
            app.dependency_overrides.pop(get_db, None)


def _fake_runner(s, year):
    """统合一个岗位→算成本→聚合（免网）；供 /compute 路由测试复用真实计算链。"""
    L_ = L
    pos = _pos()
    c = L_.compute_position_cost(s, pos, year)
    agg = positions.aggregate_to_finance(s, [c], year)
    return {"year": year, "positions_fetched": 1, "companies_computed": agg}

# ---- issue #154：英国学徒税 >£3m（隐藏公式补齐） ----
def test_apprenticeship_levy_below_allowance_zero():
    assert L.apprenticeship_levy(2_999_999.0) == 0.0
    assert L.apprenticeship_levy(3_000_000.0) == 0.0


def test_apprenticeship_levy_above_allowance():
    # (4m − 3m) × 0.5% = 5,000
    assert abs(L.apprenticeship_levy(4_000_000.0) - 5_000.0) < 1e-6


def test_apprenticeship_levy_params_override():
    p = {"levy_allowance": 1_000_000.0, "levy_pct": 1.0}
    assert abs(L.apprenticeship_levy(2_000_000.0, p) - 10_000.0) < 1e-6


def test_uk_nic_formula_includes_levy():
    base = {"nic_threshold": 9_100.0, "nic_pct": 13.8, "pension_pct": 3.0, "wc_pct": 0.5}
    salary = 4_000_000.0
    without = (salary - 9_100.0) * 0.138 + salary * 0.03 + salary * 0.005
    assert abs(L.employer_social_cost("uk_nic", salary, base)
               - (without + 5_000.0)) < 1e-6


def test_rules_payload_hides_levy_details():
    payload = L.rules_payload()
    blob = str(payload)
    assert "学徒" not in blob and "levy" not in blob.lower()
