"""四轮审计 P0 修复回归（issue #160-#164）。

- #160 PDF 中文：产物字体资源必须含 CJK CID 字体（STSong-Light），杜绝 notdef 方块假绿
- #161 换汇正向汇率行 rate=0/负 → 视缺失 422，两账户余额不变
- #162 currency_from 中文币种词+缩写配对优先（多币种标题不再误判）
- #163 return_table 复合年化附录不再污染末年（每年集满 R1-R5 封盘）
- #164 movie link / stock associate 跨币关联 422、同币放行
"""
from __future__ import annotations

import re
import zlib
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, Integer, create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.app import app
from app.api.deps import get_db
from app.db import Base
from app.export import pdf as pdf_mod
from app.ingest.normalize import currency_from
from app.ingest.parsers import parse_return_table
from app.model import (Account, Entity, ExchangeRate, LedgerEntry, MovieEvent,
                       StockEvent)


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


# ---- #160 PDF 中文字体 ----
class TestPdfCjk:
    def test_pdf_registers_cjk_font(self):
        class FakeQ:
            def scalar(self): return 3
            def __iter__(self): return iter([])

        class FakeDB:
            def execute(self, *a, **k): return FakeQ()

        orig_series, orig_tl = pdf_mod.family_total_series, pdf_mod.effective_timeline
        pdf_mod.family_total_series = lambda db: [(1990, 100.0), (1991, 250.0)]
        pdf_mod.effective_timeline = lambda db: []
        try:
            data = pdf_mod.render_pdf(FakeDB())
        finally:
            pdf_mod.family_total_series = orig_series
            pdf_mod.effective_timeline = orig_tl
        fonts = set(re.findall(rb"/BaseFont\s*/([A-Za-z0-9\-]+)", data))
        assert b"STSong-Light" in fonts, f"CJK 字体未注册，fonts={fonts}"
        # 不再是纯 Helvetica 族产物（修复前实测仅 Helvetica/Helvetica-Bold/Times-Roman/ZapfDingbats）
        assert any(b"Helvetica" not in f for f in fonts)

    def test_register_idempotent(self):
        pdf_mod._register_cjk_font()
        pdf_mod._register_cjk_font()   # 二次注册不抛


# ---- #161 换汇正向 rate=0/负防御 ----
class TestTransferZeroRate:
    def test_zero_positive_rate_row_rejected(self, db):
        from app.core.invest import ValidationError
        from app.core.transfer import transfer
        e = Entity(entity_type="person", name="T161")
        db.add(e)
        db.flush()
        src = Account(entity_id=e.id, currency="BEF")
        dst = Account(entity_id=e.id, currency="EUR")
        db.add_all([src, dst])
        db.flush()
        db.add(LedgerEntry(account_id=src.id, date=date(1999, 6, 1),
                           reason="期初", inflow=1000, balance=1000, kind="income"))
        db.flush()
        # 非法正向行：BEF→EUR rate=0（修复前 target_amount=amt×0=0 资金蒸发）
        db.add(ExchangeRate(fx_from="BEF", fx_to="EUR", year=1999, rate=0))
        db.commit()
        with pytest.raises(ValidationError) as ei:
            transfer(db, source_account_id=src.id, target_entity_id=e.id,
                     target_currency="EUR", amount=400, year=1999)
        assert "汇率" in str(ei.value)
        db.rollback()
        # 两账户余额均不变
        assert len(db.execute(select(LedgerEntry).where(
            LedgerEntry.account_id == src.id)).scalars().all()) == 1
        assert not db.execute(select(LedgerEntry).where(
            LedgerEntry.account_id == dst.id)).scalars().all()

    def test_negative_positive_rate_row_rejected(self, db):
        from app.core.invest import ValidationError
        from app.core.transfer import transfer
        e = Entity(entity_type="person", name="T161b")
        db.add(e)
        db.flush()
        src = Account(entity_id=e.id, currency="BEF")
        db.add(src)
        db.flush()
        db.add(LedgerEntry(account_id=src.id, date=date(1999, 6, 1),
                           reason="期初", inflow=1000, balance=1000, kind="income"))
        db.add(ExchangeRate(fx_from="BEF", fx_to="EUR", year=1999, rate=-40))
        db.commit()
        with pytest.raises(ValidationError):
            transfer(db, source_account_id=src.id, target_entity_id=e.id,
                     target_currency="EUR", amount=400, year=1999)


# ---- #162 currency_from 配对优先 ----
class TestCurrencyFromPairs:
    def test_multicurrency_titles(self):
        # 真实《模版.md》两节标题（修复前分别误判 BEF/DKK）
        assert currency_from("五、欧元 EUR（2002年BEF+NLG结转）") == "EUR"
        assert currency_from("六、美元 USD（DKK+SEK 1991年底转入）") == "USD"

    def test_legacy_single_abbr_title(self):
        assert currency_from("## 一、BEF（祖父）") == "BEF"
        assert currency_from("二、SEK（祖母）") == "SEK"

    def test_no_currency_returns_none(self):
        assert currency_from("三、附注说明") is None
        assert currency_from("") is None


# ---- #163 return_table 封盘 ----
class TestReturnTableCap:
    def test_composite_annex_not_polluting_last_year(self, tmp_path: Path):
        p = tmp_path / "1999-2025 香港R1-R5投资风险分级收益测算表.md"
        p.write_text(
            "# 香港市场R1-R5投资风险分级收益测算（1999–2025）\n"
            "#### 2024（上年）\n"
            "R1：1.00｜R2：2.00｜R3：3.00｜R4：4.00｜R5：5.00\n"
            "#### 2025（全球降息周期开启）\n"
            "R1：2.05｜R2：5.17｜R3：7.64｜R4：14.29｜R5：16.71｜备注：真实行\n"
            "\n## 五、分阶段复合年化收益测算\n"
            "### 阶段1：1999–2007（9年）\n"
            "R1：2.46%｜R2：3.67%｜R3：10.14%｜R4：12.07%｜R5：18.62%\n"
            "### 全周期：1999–2025（27年完整周期）\n"
            "R1：1.49%｜R2：3.61%｜R3：6.27%｜R4：3.55%｜R5：9.85%\n",
            encoding="utf-8")
        rows = parse_return_table(p)
        y2025 = [r for r in rows if r["year"] == 2025]
        y2024 = [r for r in rows if r["year"] == 2024]
        assert len(y2025) == 5, f"复合年化污染末年：{y2025}"
        assert {r["rate"] for r in y2025} == {2.05, 5.17, 7.64, 14.29, 16.71}
        assert len(y2024) == 5 and all(r["risk_lvl"] in {"R1", "R2", "R3", "R4", "R5"} for r in rows)

    def test_format_b_still_complete(self, tmp_path: Path):
        """欧洲式逐行格式不受封盘影响。"""
        p = tmp_path / "1983-2025 英国R1-R5投资风险分级收益测算表.md"
        lines = ["# 英国测算", "#### 1983（起点）"]
        lines += [f"- R{i}：{i}.1%" for i in range(1, 6)]
        lines += ["### 附录：全周期", "- R1：9.9%"]   # 同年重复档应被忽略
        p.write_text("\n".join(lines), encoding="utf-8")
        rows = parse_return_table(p)
        assert len(rows) == 5 and sum(1 for r in rows if r["risk_lvl"] == "R1") == 1


# ---- #164 同币种关联铁律 ----
class TestSameCurrencyLink:
    @pytest.fixture(autouse=True)
    def _seed(self, db):
        self.db = db
        e = Entity(entity_type="person", name="T164")
        db.add(e)
        db.flush()
        self.bef_acc = Account(entity_id=e.id, currency="BEF")
        self.usd_acc = Account(entity_id=e.id, currency="USD")
        db.add_all([self.bef_acc, self.usd_acc])
        db.flush()
        self.movie = MovieEvent(title="泰坦尼克号", currency="USD",
                                investment_date=date(1995, 6, 1),
                                investment_total=100.0)
        self.stock_evt = StockEvent(company="虎牙", date=date(2018, 5, 7),
                                    event_type="buy", shares=10, unit_price=2.0,
                                    currency="USD")
        db.add_all([self.movie, self.stock_evt])
        db.commit()
        app.dependency_overrides[get_db] = lambda: db
        yield
        app.dependency_overrides.pop(get_db, None)

    def _client(self):
        return TestClient(app)

    def test_movie_cross_currency_link_422(self):
        with self._client() as c:
            r = c.post(f"/api/v1/movie-events/{self.movie.id}/link",
                       json={"account_id": self.bef_acc.id})
            assert r.status_code == 422
            assert "BEF" in r.json()["detail"] and "USD" in r.json()["detail"]
            # 未写任何 ledger、未置 linked
            assert not db_ledger(self.db, self.bef_acc.id)
            assert self.db.get(MovieEvent, self.movie.id).linked_account_id is None

    def test_movie_same_currency_link_ok(self):
        with self._client() as c:
            r = c.post(f"/api/v1/movie-events/{self.movie.id}/link",
                       json={"account_id": self.usd_acc.id})
            assert r.status_code == 200 and r.json()["linked"] is True

    def test_movie_missing_account_404(self):
        with self._client() as c:
            assert c.post(f"/api/v1/movie-events/{self.movie.id}/link",
                          json={"account_id": 999999}).status_code == 404

    def test_stock_associate_cross_currency_422(self):
        with self._client() as c:
            r = c.post("/api/v1/stock-events/associate",
                       json={"stock_event_id": self.stock_evt.id,
                             "entity_id": self.usd_acc.entity_id,
                             "account_id": self.bef_acc.id})
            assert r.status_code == 422
            assert self.db.get(StockEvent, self.stock_evt.id).linked_account_id is None

    def test_stock_associate_same_currency_ok(self):
        with self._client() as c:
            r = c.post("/api/v1/stock-events/associate",
                       json={"stock_event_id": self.stock_evt.id,
                             "entity_id": self.usd_acc.entity_id,
                             "account_id": self.usd_acc.id})
            assert r.status_code == 200 and r.json().get("associated") is True


def db_ledger(db, account_id):
    return db.execute(select(LedgerEntry).where(
        LedgerEntry.account_id == account_id)).scalars().all()


# ---- 五轮审计 #175/#176 回归 ----
class TestReverseNegativeRate:
    def test_reverse_negative_rate_row_rejected(self, db):
        """反向行 rate=-40 → 取倒数仍非法 → 422（修复前 -1/40 负流水入账）。"""
        from app.core.invest import ValidationError
        from app.core.transfer import transfer
        e = Entity(entity_type="person", name="T175")
        db.add(e); db.flush()
        src = Account(entity_id=e.id, currency="EUR")
        db.add(src); db.flush()
        db.add(LedgerEntry(account_id=src.id, date=date(1999, 6, 1),
                           reason="期初", inflow=100, balance=100, kind="income"))
        # 仅反向行：BEF→EUR rate=-40；请求方向 EUR→BEF
        db.add(ExchangeRate(fx_from="BEF", fx_to="EUR", year=1999, rate=-40))
        db.commit()
        with pytest.raises(ValidationError):
            transfer(db, source_account_id=src.id, target_entity_id=e.id,
                     target_currency="BEF", amount=10, year=1999)

    def test_available_pairs_excludes_nonpositive(self, db):
        from app.core.transfer import available_fx_pairs
        db.add(ExchangeRate(fx_from="AAA", fx_to="BBB", year=1999, rate=0))
        db.add(ExchangeRate(fx_from="CCC", fx_to="DDD", year=1999, rate=-5))
        db.add(ExchangeRate(fx_from="EEE", fx_to="FFF", year=1999, rate=2))
        db.commit()
        pairs = available_fx_pairs(db, 1999)
        assert ("EEE", "FFF") in pairs
        assert ("AAA", "BBB") not in pairs and ("CCC", "DDD") not in pairs


class TestClosedAccountGate:
    def test_movie_link_to_closed_account_422(self, db):
        e = Entity(entity_type="person", name="C176a")
        db.add(e); db.flush()
        acc = Account(entity_id=e.id, currency="USD", status="closed",
                      closed_on=date(2003, 1, 1))
        db.add(acc)
        m = MovieEvent(title="closed测试", currency="USD",
                       investment_date=date(1995, 6, 1), investment_total=50.0)
        db.add(m); db.commit()
        with _client(db) as c:
            r = c.post(f"/api/v1/movie-events/{m.id}/link", json={"account_id": acc.id})
            assert r.status_code == 422 and "关池" in r.json()["detail"]

    def test_stock_buy_on_closed_account_422(self, db):
        e = Entity(entity_type="person", name="C176b")
        db.add(e); db.flush()
        acc = Account(entity_id=e.id, currency="USD", status="closed",
                      closed_on=date(2003, 1, 1))
        db.add(acc); db.commit()
        with _client(db) as c:
            r = c.post("/api/v1/stock-events/buy", json={
                "entity_id": e.id, "company": "X", "date": "2018-05-07",
                "unit_price": 2.0, "shares": 10, "event_id": "ui-closed-1",
                "account_id": acc.id})
            assert r.status_code == 422 and "关池" in r.json()["detail"]


# ---- 八轮审计 #189：transfer nonce 幂等 API 链路可达 ----
class TestTransferNonceIdempotent:
    def _seed(self, db):
        # 目标用另一实体（同实体同币种会被 primary_account 解析回源账户自身）
        e_src = Entity(entity_type="person", name="T189s")
        e_dst = Entity(entity_type="person", name="T189d")
        db.add_all([e_src, e_dst]); db.flush()
        src = Account(entity_id=e_src.id, currency="EUR")
        dst = Account(entity_id=e_dst.id, currency="EUR")   # 同币种=划拨路径，无需汇率
        db.add_all([src, dst]); db.flush()
        db.add(LedgerEntry(account_id=src.id, date=date(1999, 6, 1),
                           reason="期初", inflow=1000, balance=1000, kind="income"))
        db.commit()
        return e_dst, src, dst

    def test_same_nonce_replay_skipped_no_double_entry(self, db):
        """同 nonce 双提交：第二次 skipped 且 ledger 仅一对分录。"""
        from app.core.transfer import transfer
        e_target, src, dst = self._seed(db)   # e_target=目标实体
        r1 = transfer(db, source_account_id=src.id, target_entity_id=e_target.id,
                      target_currency="EUR", amount=100, year=1999,
                      nonce="abc123def456")
        assert r1["skipped"] is not True
        r2 = transfer(db, source_account_id=src.id, target_entity_id=e_target.id,
                      target_currency="EUR", amount=100, year=1999,
                      nonce="abc123def456")
        assert r2["skipped"] is True
        # 仅一对分录（源 outflow + 目标 inflow 各一笔）
        n_src = len(db.execute(select(LedgerEntry).where(
            LedgerEntry.account_id == src.id)).scalars().all())
        n_dst = len(db.execute(select(LedgerEntry).where(
            LedgerEntry.account_id == dst.id)).scalars().all())
        assert n_src == 2 and n_dst == 1   # 源=期初+转出；目标=转入

    def test_different_nonce_both_post(self, db):
        """不同 nonce（新操作）正常各自入账——幂等不误伤。"""
        from app.core.transfer import transfer
        e_target, src, dst = self._seed(db)   # e_target=目标实体
        for i, nc in enumerate(["aaa111", "bbb222"]):
            r = transfer(db, source_account_id=src.id, target_entity_id=e_target.id,
                         target_currency="EUR", amount=10, year=1999, nonce=nc)
            assert r["skipped"] is not True
        n_dst = len(db.execute(select(LedgerEntry).where(
            LedgerEntry.account_id == dst.id)).scalars().all())
        assert n_dst == 2

    def test_api_nonce_passthrough_and_skipped_shortcircuit(self, db):
        """API 层透传客户端 nonce；skipped 不产生 recompute-done（short-circuit）。"""
        from fastapi.testclient import TestClient
        from app.model import Notification
        e_src = Entity(entity_type="person", name="T189api")
        e_dst = Entity(entity_type="person", name="T189apiD")
        db.add_all([e_src, e_dst]); db.flush()
        src = Account(entity_id=e_src.id, currency="EUR")
        db.add(Account(entity_id=e_dst.id, currency="EUR"))   # 目标主体需有该币种账户
        db.add(src); db.flush()
        db.add(LedgerEntry(account_id=src.id, date=date(1999, 6, 1),
                           reason="期初", inflow=500, balance=500, kind="income"))
        db.commit()
        body = {"source_account_id": src.id, "target_entity_id": e_dst.id,
                "target_currency": "EUR", "amount": 50, "year": 1999,
                "nonce": "replay999999"}
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as c:
                r1 = c.post("/api/v1/transfers", json=body)
                assert r1.status_code == 200 and r1.json()["status"] == "ok"
                n_after_first = len(db.query(Notification).all())
                r2 = c.post("/api/v1/transfers", json=body)   # 同 nonce 重放
                assert r2.status_code == 200 and r2.json()["status"] == "skipped"
            assert len(db.query(Notification).all()) == n_after_first   # 无新通知
        finally:
            app.dependency_overrides.pop(get_db, None)
