"""回归测试：issue #80（finance_entry 生产写入）、#81（解锁重输+消除双扣）、#82（redeemed 按笔防重）。"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import BigInteger, Integer, create_engine
from sqlalchemy.orm import sessionmaker

from app.core.invest import (
    ConflictError, create_investment, redeem_investment, unlock_investment,
)
from app.core.snapshot import account_balance_at
from app.db import Base
from app.ingest import writer
from app.model import (
    Account, Entity, FinanceEntry, LedgerEntry, ReturnCurve, TimelineEvent,
)


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


def _seed(session):
    h = Entity(entity_type="person", name="Henri Peeters")
    session.add(h)
    session.flush()
    a = Account(entity_id=h.id, currency="BEF")
    session.add(a)
    session.flush()
    session.add(LedgerEntry(account_id=a.id, date=date(1974, 1, 1),
                            inflow=1000, balance=1000, kind="income", reason="初始现金"))
    # 同一主体同一年不同地区（欧洲→比利时、英国→英国），验证同年多地区互不阻塞
    session.add(ReturnCurve(country="比利时", risk_lvl="R3", year=1985, rate=10.0))
    session.add(ReturnCurve(country="英国", risk_lvl="R3", year=1985, rate=12.0))
    session.flush()
    return h.id, a.id


def _create(session, eid, region, amount=100, start="1985-05-01"):
    inv = create_investment(session, year=1985, region=region, risk_lvl="R3",
                            start_date=date.fromisoformat(start),
                            allocs=[{"entity_id": eid, "currency": "BEF", "amount": amount}])
    session.flush()
    return inv


# ---------- #82：赎回按笔防重（同年多地区互不阻塞 + redeemed 标记） ----------
def test_same_year_two_regions_redeem_independently(session):
    eid, aid = _seed(session)
    eu = _create(session, eid, "欧洲")            # 1985 欧洲 R3（country=比利时）
    uk = _create(session, eid, "英国")            # 1985 英国 R3（country=英国）
    # 各笔独立赎回：互不阻塞（旧实现按年扫全库 pool 会误伤）
    redeem_investment(session, eu)
    session.flush()
    redeem_investment(session, uk)               # 不应 409（issue #82 修复前会误伤）
    session.flush()
    assert eu.redeemed_at is not None and uk.redeemed_at is not None


def test_redeem_double_409_by_redeemed(session):
    eid, aid = _seed(session)
    inv = _create(session, eid, "欧洲")
    redeem_investment(session, inv)
    session.flush()
    with pytest.raises(ConflictError):
        redeem_investment(session, inv)


# ---------- #81：解锁重输（抹除旧写入 + 恢复 as-of + 拒绝已赎回） ----------
def test_unlock_wipes_writes_and_restores_balance(session):
    eid, aid = _seed(session)
    inv = _create(session, eid, "欧洲")
    session.flush()
    assert session.query(LedgerEntry).filter(LedgerEntry.kind == "investment").count() == 1
    assert session.query(FinanceEntry).filter(FinanceEntry.kind == "investment").count() == 1
    assert session.query(TimelineEvent).filter(TimelineEvent.overlay.is_(True)).count() == 1
    assert float(account_balance_at(session, aid, date(1985, 6, 30))) == pytest.approx(900)

    unlock_investment(session, inv)
    session.flush()
    # 划出/镜像/overlay 全部抹除，余额恢复 to 投资前
    assert session.query(LedgerEntry).filter(LedgerEntry.kind == "investment").count() == 0
    assert session.query(FinanceEntry).filter(FinanceEntry.kind == "investment").count() == 0
    assert session.query(TimelineEvent).filter(TimelineEvent.overlay.is_(True)).count() == 0
    assert float(account_balance_at(session, aid, date(1985, 6, 30))) == pytest.approx(1000)
    assert inv.locked is False


def test_overwrite_after_unlock_no_double_deduct(session):
    eid, aid = _seed(session)
    inv1 = _create(session, eid, "欧洲", amount=100)
    session.flush()
    unlock_investment(session, inv1)
    session.flush()
    # 重输 → unlocked 覆盖分支重建；账户只被划出一次（非两倍）
    inv2 = _create(session, eid, "欧洲", amount=100)
    session.flush()
    invest_rows = session.query(LedgerEntry).filter(LedgerEntry.kind == "investment").all()
    assert len(invest_rows) == 1                     # 仅新一批，无双扣
    assert float(invest_rows[0].outflow) == pytest.approx(100)
    assert float(account_balance_at(session, aid, date(1985, 6, 30))) == pytest.approx(900)
    # 注：inv1.id 与 inv2.id 在 SQLite 会复用（测试用内存库 PK 可回收），
    #     后在 Postgres BIGSERIAL 序列永不复用——此处不比较 id，只核功能。


def test_unlock_redeemed_refused(session):
    eid, aid = _seed(session)
    inv = _create(session, eid, "欧洲")
    redeem_investment(session, inv)
    session.flush()
    with pytest.raises(ConflictError):
        unlock_investment(session, inv)


# ---------- #80：finance_entry 生产写入 ----------
def test_invest_finance_entry_ui(session):
    eid, aid = _seed(session)
    inv = _create(session, eid, "欧洲", amount=100)
    session.flush()
    rows = session.query(FinanceEntry).filter(FinanceEntry.source == "ui").all()
    assert any(r.kind == "investment" and r.source == "ui" and r.year == 1985 for r in rows)


def test_redeem_finance_entry_ui(session):
    eid, aid = _seed(session)
    inv = _create(session, eid, "欧洲", amount=100)
    redeem_investment(session, inv)
    session.flush()
    kinds = {r.kind for r in session.query(FinanceEntry).filter(FinanceEntry.source == "ui").all()}
    assert {"investment", "pool", "investment_income"} <= kinds


def test_ingest_mirrors_finance_entry_salary(session):
    eid = _seed(session)[0]
    stats = writer.import_salary(session, [{
        "holder": "Henri Peeters", "currency": "BEF", "year": 1985,
        "after_tax": 5000, "source_file": "基准/薪资/养父.md",
    }])
    session.flush()
    assert stats["stream"] == 1
    fe = session.query(FinanceEntry).filter(FinanceEntry.source == "file").all()
    assert len(fe) == 1
    assert fe[0].kind == "income" and fe[0].year == 1985 and fe[0].amount == 5000


def test_ingest_mirrors_finance_entry_household_expense(session):
    _seed(session)
    stats = writer.import_household_expense(session, [{
        "holder": "Henri Peeters", "currency": "BEF", "year": 1990, "amount": 800,
        "source_file": "基准/1974-2001家庭支出.md",
    }])
    session.flush()
    fe = session.query(FinanceEntry).filter(
        FinanceEntry.kind == "expense", FinanceEntry.source == "file").all()
    assert len(fe) == 1
    assert fe[0].amount == 800 and fe[0].year == 1990