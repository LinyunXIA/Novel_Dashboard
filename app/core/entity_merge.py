"""职称别名 person 实体合并（issue #136 存量修复）。

背景：writer 修复前，收益/薪资文件曾把 holder 职称（养祖父/养父…）原样 upsert 成
别名 person 实体，与人物档案的规范实体（Henri Peeters/Joren Peeters…）形成
「同一人账务锚分裂」——收益挂别名、账户挂规范名，且 conflict H2 按规范 id
查不到既有行、失配告警不可达。

writer 侧已接 TITLE_ENTITY 归一（issue #136 前半），本模块负责存量收口：
把别名实体的全部引用改挂规范实体后删除别名行。

- 引用表：account / initial_asset / income_stream / holding_event /
  finance_entry / investment_alloc / relationship(from,to)
- account UNIQUE(entity_id, currency, bank) 冲突时：ledger 并入规范同名账户后删别名账户
- 合并完成后由调用方重算 + 重建快照（CLI 已自动执行）
- 幂等：二次运行无别名可并 → 全零统计；identity 映射名（养祖母→养祖母）不视为别名

安全边界：仅接受 TITLE_ENTITY **精确**键命中，或「职称（资产归属注解）」变体
（历史形态：旧源文件惠民租房.md（#211 已删除）的 持有人物 列，注解描述资产归属、
租金仍归持有人本人；存量库仍可能存在该类别名实体故合并逻辑保留）；
不做通用前缀模糊匹配，防止误吞普通人物名。
"""
from __future__ import annotations

from sqlalchemy import select

from app.ingest.holders import TITLE_ENTITY
from app.model import (Account, Entity, FinanceEntry, HoldingEvent, IncomeStream,
                       InitialAsset, InvestmentAlloc, LedgerEntry, Relationship)


def _resolve_alias_name(name: str) -> str | None:
    """别名判定：精确键命中，或「职称（资产归属注解）」变体（如 养祖母（资产归…））。

    只接受这两种形态，不做通用前缀模糊匹配，防止误吞普通人物名。
    返回规范名；非别名返回 None。
    """
    n = (name or "").strip()
    canon = TITLE_ENTITY.get(n)
    if canon and canon != n:
        return canon
    base = n.split("（", 1)[0].strip()
    if base != n:
        canon = TITLE_ENTITY.get(base)
        if canon and canon != n:
            return canon
    return None


def _alias_persons(session) -> list[Entity]:
    """别名 person 列表（精确键或括号注解变体）。"""
    out = []
    for e in session.execute(
            select(Entity).where(Entity.entity_type == "person")).scalars().all():
        if _resolve_alias_name(e.name):
            out.append(e)
    return out


def merge_alias_persons(session, log=None, dry_run: bool = False) -> dict:
    """把别名 person 的引用并入规范实体并删除别名；返回统计。

    dry_run=True 只报告将合并的名单，不做任何写操作。
    """
    log = log or (lambda m: None)
    aliases = _alias_persons(session)
    if dry_run:
        return {"dry_run": True,
                "would_merge": [{"alias": a.name,
                                 "canonical": _resolve_alias_name(a.name)}
                                for a in aliases]}

    stats = {"merged": 0, "accounts_moved": 0, "ledger_moves": 0, "initial_assets": 0,
             "income_streams": 0, "holding_events": 0, "finance_entries": 0,
             "allocs": 0, "relationships": 0}
    for alias in aliases:
        canon_name = _resolve_alias_name(alias.name)
        canon = session.execute(
            select(Entity).where(Entity.entity_type == "person", Entity.name == canon_name)
        ).scalar_one_or_none()
        if canon is None:
            canon = Entity(entity_type="person", name=canon_name)
            session.add(canon)
            session.flush()
        if canon.id == alias.id:            # 理论不可达（canon != alias.name）
            continue

        # —— 账户：唯一键冲突则并入规范同名账户 ——
        for acc in session.execute(
                select(Account).where(Account.entity_id == alias.id)).scalars().all():
            twin = session.execute(
                select(Account).where(Account.entity_id == canon.id,
                                      Account.currency == acc.currency,
                                      Account.bank == acc.bank).limit(1)
            ).scalar_one_or_none()
            if twin is None or twin.id == acc.id:
                acc.entity_id = canon.id
                stats["accounts_moved"] += 1
            else:
                n = session.execute(
                    select(LedgerEntry).where(LedgerEntry.account_id == acc.id)
                ).scalars().all()
                for le in n:
                    le.account_id = twin.id
                stats["ledger_moves"] += len(n)
                session.delete(acc)

        # —— 其余 FK 批量改挂 ——
        for model, col, key in (
                (InitialAsset, InitialAsset.entity_id, "initial_assets"),
                (IncomeStream, IncomeStream.entity_id, "income_streams"),
                (HoldingEvent, HoldingEvent.entity_id, "holding_events"),
                (FinanceEntry, FinanceEntry.entity_id, "finance_entries"),
                (InvestmentAlloc, InvestmentAlloc.entity_id, "allocs")):
            n = session.query(model).filter(col == alias.id).update(
                {col: canon.id}, synchronize_session=False)
            stats[key] += n
        n = session.query(Relationship).filter(
            Relationship.from_entity_id == alias.id).update(
            {Relationship.from_entity_id: canon.id}, synchronize_session=False)
        n2 = session.query(Relationship).filter(
            Relationship.to_entity_id == alias.id).update(
            {Relationship.to_entity_id: canon.id}, synchronize_session=False)
        stats["relationships"] += n + n2
        # 改挂后可能产生自环（别名↔规范互指）→ 删除
        for rel in session.execute(
                select(Relationship).where(Relationship.from_entity_id ==
                                           Relationship.to_entity_id)).scalars().all():
            session.delete(rel)

        session.delete(alias)
        stats["merged"] += 1
        log(f"   ♻ 别名实体「{alias.name}」→「{canon_name}」并入完成")
    session.flush()
    return stats
