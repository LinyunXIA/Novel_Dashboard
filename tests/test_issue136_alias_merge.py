"""issue #136 回归：收益归属名经 TITLE_ENTITY 归一 + 存量别名实体合并。

writer 侧：holder 职称（养祖父/养父…）必须挂规范实体（Henri Peeters/Joren Peeters），
不再新建职称别名 person；merge_alias_persons 把历史别名引用并入规范实体后删除别名，
account UNIQUE 冲突时并入同名账户，且幂等。
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import BigInteger, Integer, create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.entity_merge import merge_alias_persons
from app.db import Base
from app.ingest import writer
from app.model import (Account, Entity, FinanceEntry, IncomeStream, InitialAsset,
                       LedgerEntry, Relationship)


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


def _names(session) -> set[str]:
    return {e.name for e in session.execute(select(Entity)).scalars().all()}


def test_writer_canonicalizes_security_holder(session):
    recs = [{"holder": "养祖父", "name": "测试券", "face_value": 1000.0,
             "currency": "BEF", "rate_pct": 4.0, "source_file": "f.md"}]
    stats = writer.import_income_security(session, recs, years=(1980, 1980))
    assert stats["stream"] == 1
    assert "养祖父" not in _names(session)
    henri = session.execute(
        select(Entity).where(Entity.name == "Henri Peeters")).scalar_one()
    row = session.execute(select(IncomeStream)).scalar_one()
    assert row.entity_id == henri.id


def test_writer_canonicalizes_salary_holder(session):
    recs = [{"holder": "养父", "year": 1990, "after_tax": 120.0,
             "currency": "BEF", "source_file": "s.md"}]
    writer.import_salary(session, recs)
    assert "养父" not in _names(session)
    joren = session.execute(
        select(Entity).where(Entity.name == "Joren Peeters")).scalar_one()
    row = session.execute(select(IncomeStream)).scalar_one()
    assert row.entity_id == joren.id
    assert row.group_key == "Joren Peeters薪资"
    fin = session.execute(select(FinanceEntry)).scalar_one()
    assert fin.label == "Joren Peeters薪资税后"


def test_writer_canonicalizes_bank_account_anchor(session):
    segs = [{"holder": "养父", "currency": "BEF", "bank": None, "rows": []}]
    st = writer.import_bank(session, segs, source_file="b.md")
    assert st["account"] == 1
    assert "养父" not in _names(session)
    joren = session.execute(
        select(Entity).where(Entity.name == "Joren Peeters")).scalar_one()
    acc = session.execute(select(Account)).scalar_one()
    assert acc.entity_id == joren.id


def test_identity_mapped_person_not_touched(session):
    session.add(Entity(entity_type="person", name="养祖母"))
    session.flush()
    r = merge_alias_persons(session, log=lambda m: None)
    assert r["merged"] == 0
    assert "养祖母" in _names(session)


def test_merge_parenthetical_variant(session):
    """「职称（资产归…）」变体（惠民租房.md 持有人物列）：并入同名规范实体。"""
    session.add(Entity(entity_type="person", name="养祖母"))
    session.flush()
    canon = session.execute(
        select(Entity).where(Entity.name == "养祖母")).scalar_one()
    variant = Entity(entity_type="person", name="养祖母（资产归Henri Peeters）")
    session.add(variant)
    session.flush()
    session.add(IncomeStream(entity_id=variant.id, stream_type="rent",
                             group_key="瑞典惠民租", currency="SEK", year=1980,
                             amount=1, label="l", source_file="rent.md"))
    session.flush()

    r = merge_alias_persons(session, log=lambda m: None)
    assert r["merged"] == 1
    assert session.get(Entity, variant.id) is None
    row = session.execute(select(IncomeStream)).scalar_one()
    assert row.entity_id == canon.id


def test_merge_alias_persons_repoints_deletes_idempotent(session):
    henri = Entity(entity_type="person", name="Henri Peeters")
    session.add(henri)
    session.flush()
    alias = Entity(entity_type="person", name="养祖父")
    session.add(alias)
    session.flush()
    acc_alias = Account(entity_id=alias.id, currency="BEF", bank=None)
    acc_henri = Account(entity_id=henri.id, currency="BEF", bank=None)
    session.add_all([acc_alias, acc_henri])
    session.flush()
    session.add(LedgerEntry(account_id=acc_alias.id, date=date(1990, 1, 1),
                            reason="x", inflow=10, kind="income"))
    session.add(IncomeStream(entity_id=alias.id, stream_type="security", group_key="g",
                             currency="BEF", year=1980, amount=1, label="l",
                             source_file="f.md"))
    session.add(FinanceEntry(entity_id=alias.id, entity_kind="person", year=1980,
                             kind="income", amount=1, currency="BEF", label="l"))
    session.add(InitialAsset(entity_id=alias.id, asset_type="bond", currency="BEF",
                             name="债券包", face_value=100))
    session.add(Relationship(from_entity_id=alias.id, to_entity_id=henri.id,
                             rel_type="member"))
    session.flush()

    r = merge_alias_persons(session, log=lambda m: None)
    assert r["merged"] == 1
    # 别名删除、引用全部改挂规范实体
    assert session.get(Entity, alias.id) is None
    assert "养祖父" not in _names(session)
    inc = session.execute(select(IncomeStream)).scalar_one()
    fin = session.execute(select(FinanceEntry)).scalar_one()
    ia = session.execute(select(InitialAsset)).scalar_one()
    assert (inc.entity_id, fin.entity_id, ia.entity_id) == (henri.id,) * 3
    # account UNIQUE 冲突路径：ledger 并入 Henri 同名账户、别名账户删除
    ledgers = session.execute(select(LedgerEntry)).scalars().all()
    assert len(ledgers) == 1 and ledgers[0].account_id == acc_henri.id
    assert session.execute(select(Account)).scalars().all()[0].entity_id == henri.id \
        or len(session.execute(select(Account)).scalars().all()) == 1
    # 改挂后的自环关系被清理
    assert session.execute(select(Relationship)).scalars().all() == []

    # 幂等：二次运行无可并别名
    r2 = merge_alias_persons(session, log=lambda m: None)
    assert r2["merged"] == 0


def test_merge_dry_run_reports_only(session):
    session.add(Entity(entity_type="person", name="Henri Peeters"))
    session.add(Entity(entity_type="person", name="养祖父"))
    session.flush()
    r = merge_alias_persons(session, dry_run=True)
    assert r["dry_run"] is True
    assert r["would_merge"] == [{"alias": "养祖父", "canonical": "Henri Peeters"}]
    assert "养祖父" in _names(session)
