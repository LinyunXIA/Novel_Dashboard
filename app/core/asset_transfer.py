"""资产转移（#206 · 图谱资产面板按业务分组转给其他个人/公司）。

选中来源实体的一个业务分组（股票债券 / 惠民租房 / 经营性房产 / 现金），把该组的：
- initial_asset 存量（按 kind 判定）
- 对应 income_stream（按 stream_type：security→股票债券、rent→惠民租房、property→经营性房产）
一并**改归属**到目标主体（person/company）。只改存量归属、不迁移历史 ledger；触发全量重算。

来源/目标可人/公司互转；回退=反向再转一次；转移记一条编年史审计事件（overlay=True）。
调用方负责 commit + recompute + 快照 + recompute-done 通知。
"""
from __future__ import annotations

import datetime as _dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.model import Entity, IncomeStream, InitialAsset, TimelineEvent


def _prop_is_rent(a: InitialAsset) -> bool:
    nm = a.name or ""
    return any(k in nm for k in ("惠民", "出租", "承租", "1/10"))


# kind → (初始资产谓词, 该组收益 flow stream_type 集合)
KIND_RULES: dict[str, tuple] = {
    "股票债券":   (lambda a: a.asset_type in ("stock", "bond"), ("security",)),
    "惠民租房":   (lambda a: a.asset_type == "property" and _prop_is_rent(a), ("rent",)),
    "经营性房产":  (lambda a: a.asset_type == "property" and not _prop_is_rent(a), ("property",)),
    "现金":      (lambda a: a.asset_type == "cash", ()),
}


class TransferError(ValueError):
    pass


def transfer_asset_group(session: Session, source_id: int, kind: str,
                         to_id: int, at_date: _dt.date | None = None) -> dict:
    """把来源实体的某业务分组资产+收益改归属到目标主体，写编年史审计事件。返回统计。"""
    src = session.get(Entity, source_id)
    tgt = session.get(Entity, to_id)
    if src is None or tgt is None:
        raise TransferError("来源/目标主体不存在")
    if src.id == tgt.id:
        raise TransferError("来源与目标不能相同")
    if tgt.entity_type not in ("person", "company"):
        raise TransferError("目标必须是 person 或 company")

    rule = KIND_RULES.get(kind)
    if rule is None:
        raise TransferError(f"未知资产分组：{kind}（应为 股票债券/惠民租房/经营性房产/现金）")
    pred, streams = rule

    ia_rows = [a for a in session.execute(
        select(InitialAsset).where(InitialAsset.entity_id == source_id)).scalars().all()
        if pred(a)]
    inc_rows = []
    if streams:
        inc_rows = session.execute(
            select(IncomeStream).where(IncomeStream.entity_id == source_id,
                                       IncomeStream.stream_type.in_(streams))
        ).scalars().all()
    if not ia_rows and not inc_rows:
        raise TransferError(f"「{src.name}」名下无「{kind}」资产可转移")

    for a in ia_rows:
        a.entity_id = tgt.id
    for i in inc_rows:
        i.entity_id = tgt.id

    at = at_date or _dt.date.today()
    session.add(TimelineEvent(
        event_year=at.year, event_date=at,
        title=f"{kind}转移：{src.name} → {tgt.name}",
        note=f"（UI 资产转移）{kind} 存量{len(ia_rows)}项 / 收益{len(inc_rows)}条 {tgt.display_name or tgt.name}",
        decade=f"{at.year // 10 * 10}s", overlay=True))
    return {"kind": kind, "assets": len(ia_rows), "income": len(inc_rows),
            "from": src.id, "to": tgt.id}