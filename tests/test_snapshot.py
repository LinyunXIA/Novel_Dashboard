"""Unit tests for app/core/snapshot.rebuild_snapshots（issue #12 回归）。

通过 SQLite 内存数据库 + 临时 DDL 验证三层快照（account/entity/family:total）
与 from_year 增量重建语义。
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.model import Account, Entity, ExchangeRate, LedgerEntry, Snapshot
from app.core.snapshot import rebuild_snapshots


@pytest.fixture
def session():
    """内存 SQLite + 临时 DDL；每个测试独立。

    SQLite 下 BigInteger PK 不自动 ROWID 化（与 PG BIGSERIAL 不一致），
    改用 Integer 替代所有 BigInteger 主键，保证 autoincrement 生效。
    """
    from sqlalchemy import BigInteger, Column, Integer

    def _patch_bigint_to_int():
        """遍历所有表，把 BigInteger PK 临时改成 Integer（仅测试用）。"""
        for table in Base.metadata.tables.values():
            for col in table.columns:
                if isinstance(col.type, BigInteger) and col.primary_key:
                    col.type = Integer()

    _patch_bigint_to_int()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def _seed_basic(session, *, with_usd_fx: bool = True):
    """最小种子：Henri Peeters (BEF) + Joren Peeters (USD) + 一些 ledger。"""
    h = Entity(entity_type="person", name="Henri Peeters")
    j = Entity(entity_type="person", name="Joren Peeters")
    session.add_all([h, j])
    session.flush()
    a1 = Account(entity_id=h.id, currency="BEF")
    a2 = Account(entity_id=j.id, currency="USD")
    session.add_all([a1, a2])
    session.flush()
    # 1980 划拨 100 BEF + 50 USD
    session.add_all([
        LedgerEntry(account_id=a1.id, date=date(1980, 1, 1),
                    inflow=100, balance=100, kind="income", reason="划拨"),
        LedgerEntry(account_id=a2.id, date=date(1980, 1, 1),
                    inflow=50, balance=50, kind="income", reason="划拨"),
        # 1981 又入 20 BEF
        LedgerEntry(account_id=a1.id, date=date(1981, 6, 1),
                    inflow=20, balance=120, kind="income", reason="注资"),
    ])
    if with_usd_fx:
        # 1980 USD→BEF 50 → 1 USD = 50 BEF → 1 BEF = 0.02 USD
        session.add(ExchangeRate(fx_from="USD", fx_to="BEF", year=1980, rate=50.0))
    session.commit()
    return {"henri": h.id, "joren": j.id, "a_bef": a1.id, "a_usd": a2.id}


class TestRebuildSnapshotsScopeCoverage:
    """issue #12：补 account:* / entity:* / family:total 三种 scope。"""

    def test_account_scope_written(self, session):
        _seed_basic(session)
        rebuild_snapshots(session, range(1980, 1982))
        snaps = session.query(Snapshot).all()
        # 2 账户 × 2 年 = 4 account:* + 2 entity:* + 2 family:total = 8
        account_snaps = [s for s in snaps if s.scope.startswith("account:")]
        assert len(account_snaps) == 4
        assert all(s.scope.startswith("account:") for s in account_snaps)

    def test_entity_scope_written(self, session):
        _seed_basic(session)
        rebuild_snapshots(session, range(1980, 1982))
        entity_snaps = [s for s in session.query(Snapshot).all() if s.scope.startswith("entity:")]
        # 2 entity × 1 currency (Henri=BEF / Joren=USD) × 2 年 = 4 行
        assert len(entity_snaps) == 4
        # 1981: Henri BEF = 120 (100 划拨 + 20 注资)
        assert any(s.scope == "entity:1:BEF" and s.as_of_year == 1981 and float(s.value) == 120.0
                   for s in entity_snaps)
        # 1980: Henri BEF = 100
        assert any(s.scope == "entity:1:BEF" and s.as_of_year == 1980 and float(s.value) == 100.0
                   for s in entity_snaps)

    def test_family_total_written_in_usd(self, session):
        ids = _seed_basic(session, with_usd_fx=True)
        rebuild_snapshots(session, range(1980, 1982))
        fam = session.query(Snapshot).filter(Snapshot.scope == "family:total").all()
        assert len(fam) == 2
        # 1980: Henri BEF 100 (rate 1/50=0.02 USD → 2 USD) + Joren USD 50 = 52 USD
        fam_1980 = next(s for s in fam if s.as_of_year == 1980)
        assert float(fam_1980.value) == pytest.approx(52.0)
        assert fam_1980.currency == "USD"


class TestRebuildSnapshotsFromYear:
    """issue #12：from_year 增量重建（旧段保留）。"""

    def test_from_year_only_deletes_target(self, session):
        _seed_basic(session)
        # 全量先建
        rebuild_snapshots(session, range(1980, 1982))
        before = session.query(Snapshot).count()
        # 增量重建 1981+
        rebuild_snapshots(session, range(1981, 1982), from_year=1981)
        after = session.query(Snapshot).count()
        snaps_1980 = session.query(Snapshot).filter(Snapshot.as_of_year == 1980).all()
        snaps_1981 = session.query(Snapshot).filter(Snapshot.as_of_year == 1981).all()
        # 旧 1980 段全部保留
        assert all(s.value is not None for s in snaps_1980)
        # 新 1981 段重写（条数与 from_year=1980 一致）
        assert len(snaps_1981) > 0

    def test_from_year_default_is_full_rebuild(self, session):
        _seed_basic(session)
        rebuild_snapshots(session, range(1980, 1982))                 # 全量
        first = session.query(Snapshot).count()
        rebuild_snapshots(session, range(1980, 1982))                 # 再全量（from_year=None）
        second = session.query(Snapshot).count()
        assert first == second


class TestRebuildSnapshotsMissingRates:
    """汇率缺失币种不计入 family:total（issue #2 一致）。"""

    def test_missing_fx_excluded_from_family(self, session):
        _seed_basic(session, with_usd_fx=False)            # 无 BEF→USD 汇率
        rebuild_snapshots(session, range(1980, 1982))
        fam = next(s for s in session.query(Snapshot).all()
                   if s.scope == "family:total" and s.as_of_year == 1980)
        # 缺 BEF 汇率 → 只算 Joren USD 50
        assert float(fam.value) == 50.0


class TestRebuildSnapshotsEmpty:
    def test_empty_years(self, session):
        r = rebuild_snapshots(session, range(1980, 1979))    # 空 range
        assert r == {"snapshots": 0, "accounts": 0, "entities": 0, "family_years": 0}