"""落库写入（F-P0-04 初始资产摄入 / 基础 entity/account）。

把 parse 结果 + 初始资产写入 DB：
- character -> entity（upsert by entity_type+name）
- initial_asset -> entity + account(by 币种) + initial_asset 表；现金 -> ledger 首笔余额
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.model import Account, Entity, IncomeStream, InitialAsset, LedgerEntry


def upsert_entity(session: Session, entity_type: str, name: str,
                  display_name: str | None = None, fields: dict | None = None,
                  source_file: str | None = None, source: str = "file") -> Entity:
    ent = session.execute(
        select(Entity).where(Entity.entity_type == entity_type, Entity.name == name)
    ).scalar_one_or_none()
    if ent is None:
        ent = Entity(entity_type=entity_type, name=name, source=source)
        session.add(ent)
    if display_name:
        ent.display_name = display_name
    if fields:
        ent.fields = {**(ent.fields or {}), **fields}
    if source_file:
        ent.source_file = source_file
    session.flush()
    return ent


def get_or_create_account(session: Session, entity_id: int, currency: str, bank: str | None = None) -> Account:
    acc = session.execute(
        select(Account).where(Account.entity_id == entity_id, Account.currency == currency)
    ).scalar_one_or_none()
    if acc is None:
        acc = Account(entity_id=entity_id, currency=currency, bank=bank)
        session.add(acc)
        session.flush()
    return acc


def import_characters(session: Session, records: list[dict], source_file: str | None = None) -> dict:
    """character 解析记录 → entity。返回 {导入数}。"""
    n = 0
    for rec in records:
        upsert_entity(session, "person", rec["name"], fields=rec.get("fields"),
                      source_file=rec.get("source_file") or source_file)
        n += 1
    return {"imported": n}


def import_initial_assets(session: Session, records: list[dict], cash_year: int = 1947) -> dict:
    """初始资产 → entity/inital_asset/account；现金进余额(ledger 首笔)。

    返回统计。
    """
    stats = {"asset": 0, "cash": 0}
    for rec in records:
        ent = upsert_entity(session, "person", rec["entity_name"])
        if rec["asset_type"] == "cash":
            # 现金 → 账户 + 首笔存款
            cur = rec["currency"]
            acc = get_or_create_account(session, ent.id, cur)
            amort = rec["face_value"]
            session.add(LedgerEntry(account_id=acc.id, date=date(cash_year, 12, 30),
                                    reason="初始现金", inflow=amort, balance=amort,
                                    kind="income", source_file=rec.get("source_file")))
            stats["cash"] += 1
        else:
            session.add(InitialAsset(
                entity_id=ent.id, asset_type=rec["asset_type"],
                group_key=rec.get("group_key"), currency=rec.get("currency"),
                name=rec.get("name"), face_value=rec.get("face_value"),
                pct=rec.get("pct"), source_file=rec.get("source_file"),
            ))
            stats["asset"] += 1
    return stats


def import_income_security(session: Session, records: list[dict],
                           years: tuple[int, int] = (1947, 2025), label_prefix: str = "祖产债券票息") -> dict:
    """祖产债券每券 → 逐年 income_stream（面值 × 票息率）。

    归属 entity = holder；币种按券；逐年票息金额=面值×rate。
    返回 {stream: 生成行}。
    """
    stats = {"stream": 0}
    for rec in records:
        holder = rec.get("holder")
        if not holder:
            continue
        ent = upsert_entity(session, "person", holder)
        rate = rec.get("rate_pct") or 0.0
        amount = (rec.get("face_value") or 0.0) * rate / 100.0
        if not amount:
            continue
        for y in range(years[0], years[1] + 1):
            session.add(IncomeStream(
                entity_id=ent.id, stream_type="security", group_key="祖产债券",
                currency=rec.get("currency"), year=y, amount=amount,
                label=f"{label_prefix} · {rec.get('name','')[:20]}",
                source_file=rec.get("source_file"),
            ))
            stats["stream"] += 1
    return stats


def _rent_factor(year: int) -> float:
    """租房分段复利系数：1974 = 1.0（基桩年不涨）；1975 起按分段年涨幅累乘。"""
    if year <= 1974:
        return 1.0
    f = 1.0
    for y in range(1975, year + 1):
        if y <= 1984:
            f *= 1.07
        elif y <= 1999:
            f *= 1.035
        else:
            f *= 1.05
    return f


def import_income_rent(session: Session, records: list[dict],
                       years: tuple[int, int] = (1974, 2007)) -> dict:
    """惠民租房 → 逐年租金 income_stream（单套年租金×套数×分段复利系数）。"""
    stats = {"stream": 0}
    for rec in records:
        ent = upsert_entity(session, "person", rec["holder"])
        base = (rec.get("unit_rent") or 0.0) * (rec.get("units") or 0.0)
        for y in range(years[0], years[1] + 1):
            if y < (rec.get("start") or 1974):
                continue
            session.add(IncomeStream(
                entity_id=ent.id, stream_type="rent", group_key=f"{rec.get('country')}惠民租",
                currency=rec.get("currency"), year=y,
                amount=round(base * _rent_factor(y), 2),
                label=f"惠民租房 · {rec.get('country')}",
                source_file=rec.get("source_file"),
            ))
            stats["stream"] += 1
    return stats