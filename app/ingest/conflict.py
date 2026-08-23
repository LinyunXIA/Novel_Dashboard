"""导入前冲突检测 hard-block（DESIGN §11.4）。

在写库前比对 新记录 vs DB 既有相关记录：
- 金额冲突(H2)：同 entity×stream_type×currency×year 已有不同金额 → 拦（不入库）
- 余额断链(H4)：账户追加流水时，新首笔前值 ≠ 已存在末余额 → 拦
- 断链/引用(H5)：回引不存在的 entity/account → 拦

命中冲突：抛 ConflictError，由上层将整文件标为"需人工"、不入库。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.model import Account, IncomeStream, LedgerEntry


class ConflictError(Exception):
    def __init__(self, file: str, rule: str, detail: str):
        super().__init__(f"[{rule}] {file}: {detail}")
        self.file = file
        self.rule = rule
        self.detail = detail


@dataclass
class ConflictReport:
    file: str
    problems: list = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.problems)

    def add(self, rule: str, line: object, desc: str):
        self.problems.append({"rule": rule, "line": line, "detail": desc})


def check_income_stream_conflict(session: Session, file: str, records: list[dict]) -> ConflictReport:
    """income_stream 金额冲突（H2）：同 (entity, stream_type, currency, year) 已存在且金额不同 → 拦。"""
    rep = ConflictReport(file)
    from app.model import Entity
    for rec in records:
        ent = session.execute(
            select(Entity.id).where(Entity.name == rec.get("entity_name", rec.get("holder", "")))
        ).scalar_one_or_none()
        if ent is None:
            rep.add("H5-引用", rec, f"entity 不存在: {rec.get('entity_name', rec.get('holder'))}")
            continue
        st = rec.get("stream_type")
        cur = rec.get("currency")
        year = rec.get("year")
        amt = rec.get("amount")
        if st is None or year is None:
            continue
        existing = session.execute(
            select(IncomeStream.amount).where(
                IncomeStream.entity_id == ent,
                IncomeStream.stream_type == st,
                IncomeStream.currency == cur,
                IncomeStream.year == year,
            )
        ).scalar_one_or_none()
        if existing is not None and existing != amt:
            rep.add("H2-金额", year, f"{st} {cur} {year} 既有 {existing} ≠ 新 {amt}")
    return rep


def check_account_balance_conflict(session: Session, file: str,
                                   account_id: int, new_entries: list[dict]) -> ConflictReport:
    """余额断链(H4)：追加流水时新首笔前值 ≠ 已存在末余额 → 拦。"""
    rep = ConflictReport(file)
    last_balance = session.execute(
        select(LedgerEntry.balance).where(LedgerEntry.account_id == account_id)
        .order_by(LedgerEntry.date.desc(), LedgerEntry.id.desc()).limit(1)
    ).scalar_one_or_none()
    if last_balance is not None and new_entries:
        first = new_entries[0]
        prev = first.get("balance")
        if prev is not None and last_balance is not None and abs(prev - last_balance) > 0.005:
            rep.add("H4-余额", first.get("date"), f"既有末余额 {last_balance} ≠ 新首笔 {prev}")
    return rep


def ensure_entity(session: Session, name: str, entity_type: str = "person") -> int | None:
    """确保 entity 存在，返回 id；不存在返回 None（供 H5 断链检查）。"""
    from app.model import Entity
    ent = session.execute(
        select(Entity.id).where(Entity.name == name, Entity.entity_type == entity_type)
    ).scalar_one_or_none()
    return int(ent) if ent is not None else None