"""Unit tests for issue #28 数值纪律：Decimal 化 + 0 值压缩。

覆盖：
- recompute_account 内部用 Decimal（balance 累计无 float 转换误差）
- snapshot 0 值年份跳过写入（account / entity / family 三种 scope）
- snapshot 写入 value 是 Decimal（Decimal('123.45'）精确位）
- 0 值 family_usd 跳过该年（避免多年 0 行膨胀）
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.recompute import recompute_account
from app.core.snapshot import rebuild_snapshots
from app.db import Base
from app.model import Account, Entity, ExchangeRate, LedgerEntry, Snapshot


@pytest.fixture
def session():
    from sqlalchemy import BigInteger, Integer
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, BigInteger) and col.primary_key:
                col.type = Integer()
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def _seed_account(session, currency="BEF") -> tuple[Entity, Account]:
    e = Entity(entity_type="person", name="Henri Peeters")
    a = Account(entity_id=0, currency=currency)
    session.add_all([e, a])
    session.flush()
    a.entity_id = e.id
    session.commit()
    return e, a


def _close_account(session, acc: Account, year: int) -> None:
    """issue #28：账户关池 → 该年起 balance=0（DESIGN §6.6）。"""
    acc.status = "closed"
    acc.closed_on = date(year, 1, 1)
    session.commit()


class TestRecomputeDecimalPrecision:
    def test_balance_uses_decimal_not_float(self, session):
        """issue #28：balance 不再经 float() 转换；用 Decimal 累加。"""
        _, a = _seed_account(session)
        # 经典 float 误差场景：0.1 + 0.2 ≠ 0.3（float=0.30000000000000004）
        # 用 Decimal(0.1)+Decimal(0.2) 应得 0.3 精确
        session.add_all([
            LedgerEntry(account_id=a.id, date=date(1990, 6, 1),
                        inflow=Decimal("0.10"), balance=Decimal("0.10")),
            LedgerEntry(account_id=a.id, date=date(1990, 7, 1),
                        inflow=Decimal("0.20")),
        ])
        session.commit()
        result = recompute_account(session, a.id, 1990)
        assert result["updated"] >= 1
        # 取第二条 ledger，balance 必须是 Decimal('0.30') 而不是 0.30000000000000004
        second = session.execute(
            LedgerEntry.__table__.select().where(LedgerEntry.id == 2)
        ).first()
        assert second.balance == Decimal("0.30"), \
            f"balance 必须精确为 0.30，实际 {second.balance} ({type(second.balance).__name__})"

    def test_no_float_intermediate(self, session):
        """验证不引入 float 中间类型（balance 写回仍是 Decimal 类型）。"""
        _, a = _seed_account(session)
        session.add(LedgerEntry(account_id=a.id, date=date(1990, 1, 1),
                                inflow=Decimal("100.50"), outflow=Decimal("30.25"),
                                balance=Decimal("70.25")))
        session.commit()
        recompute_account(session, a.id, 1990)
        e = session.execute(
            LedgerEntry.__table__.select().where(LedgerEntry.account_id == a.id)
        ).first()
        # 类型必须是 Decimal 而非 float
        assert isinstance(e.balance, Decimal), \
            f"balance 必须是 Decimal 类型，实际 {type(e.balance).__name__}"


class TestSnapshotZeroSkip:
    def test_account_zero_years_skipped_after_close(self, session):
        """issue #28：账户关池后 balance=0 → 该年及之后快照跳过。

        关池年 1995 → 1995/1996/1997 都应跳过（balance 全 0），仅写 1990-1994。
        """
        _, a = _seed_account(session)
        session.add(LedgerEntry(account_id=a.id, date=date(1990, 12, 30),
                                inflow=Decimal("100"), balance=Decimal("100")))
        session.commit()
        _close_account(session, a, 1995)
        rebuild_snapshots(session, years=range(1990, 1998))
        rows = session.query(Snapshot).filter(
            Snapshot.scope.like("account:%")
        ).all()
        # 关池前 5 年（1990-1994）+ 关池年本身（1995 balance=0 跳过）→ 5 行
        years = sorted(r.as_of_year for r in rows)
        assert years == [1990, 1991, 1992, 1993, 1994], \
            f"关池年起应跳过 0 值行，实际 {years}"

    def test_entity_zero_years_skipped_after_close(self, session):
        """entity scope 同样：账户关池后 entity 的该年聚合为 0 → 跳过。"""
        _, a = _seed_account(session)
        session.add(LedgerEntry(account_id=a.id, date=date(1990, 12, 30),
                                inflow=Decimal("100"), balance=Decimal("100")))
        session.commit()
        _close_account(session, a, 1993)
        rebuild_snapshots(session, years=range(1990, 1995))
        rows = session.query(Snapshot).filter(
            Snapshot.scope.like("entity:%")
        ).all()
        assert sorted(r.as_of_year for r in rows) == [1990, 1991, 1992]

    def test_family_zero_year_skipped(self, session):
        """family:total 0 值年跳过（无任何有值账户贡献时不写）。"""
        # 不放 ledger，纯空账户
        e = Entity(entity_type="person", name="Empty Holder")
        session.add(e)
        session.flush()
        a = Account(entity_id=e.id, currency="BEF")
        session.add(a)
        session.commit()
        # 加 USD→BEF 汇率，让 family 计算时不为 None
        session.add(ExchangeRate(fx_from="USD", fx_to="BEF", year=2000, rate=40.0))
        session.commit()
        rebuild_snapshots(session, years=range(2000, 2003))
        rows = session.query(Snapshot).filter(Snapshot.scope == "family:total").all()
        # 账户无流水，所有年 family_usd=0 → 全部跳过
        assert rows == [], f"family 0 值年应全部跳过，实际 {[(r.as_of_year, r.value) for r in rows]}"

    def test_family_written_when_any_account_has_value(self, session):
        """有任一账户在该年有余额 → family 写一行；该年累计 0 则跳过。

        2000 入金 500（balance=500）→ family=500；2001 起无流水（2001 累计仍 500）→
        family=500 也写。预期两行（2000/2001 都非零）。
        """
        _, a = _seed_account(session, currency="USD")
        session.add(LedgerEntry(account_id=a.id, date=date(2000, 12, 30),
                                inflow=Decimal("500"), balance=Decimal("500")))
        session.commit()
        rebuild_snapshots(session, years=range(2000, 2002))
        rows = session.query(Snapshot).filter(Snapshot.scope == "family:total").all()
        # 累计非零的所有年都写
        assert len(rows) == 2
        assert rows[0].as_of_year == 2000
        assert rows[0].value == Decimal("500.00")
        assert rows[1].as_of_year == 2001
        assert rows[1].value == Decimal("500.00")


class TestSnapshotValueIsDecimal:
    def test_written_value_is_decimal(self, session):
        """issue #28：写库 value 是 Decimal 而非 float（保证 NUMERIC 列精度）。"""
        _, a = _seed_account(session)
        session.add(LedgerEntry(account_id=a.id, date=date(1990, 12, 30),
                                inflow=Decimal("123.45"), balance=Decimal("123.45")))
        session.commit()
        rebuild_snapshots(session, years=range(1990, 1991))
        row = session.query(Snapshot).first()
        assert isinstance(row.value, Decimal), \
            f"value 必须是 Decimal 类型，实际 {type(row.value).__name__}"
        assert row.value == Decimal("123.45")