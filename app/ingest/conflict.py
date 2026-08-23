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


def _resolve_entity_id(session: Session, name: str | None) -> int | None:
    """按登记名解析 entity id（issue #68）：

    - 先精确匹配；
    - 再走 holders.TITLE_ENTITY 别名归一（如「养父」→「Joren Peeters」、
      「祖父」→「Henri Peeters」），与 writer 侧持有人口径一致。
    """
    from app.ingest.holders import holder_entity_name
    from app.model import Entity
    if not name:
        return None
    hit = session.execute(
        select(Entity.id).where(Entity.name == name)
    ).scalar_one_or_none()
    if hit is not None:
        return int(hit)
    canon = holder_entity_name(name)
    if canon and canon != name:
        hit = session.execute(
            select(Entity.id).where(Entity.name == canon)
        ).scalar_one_or_none()
        if hit is not None:
            return int(hit)
    return None


def check_income_stream_conflict(session: Session, file: str, records: list[dict]) -> ConflictReport:
    """income_stream 金额冲突（H2）：同 (entity, stream_type, currency, year) 已存在且金额不同 → 拦。"""
    rep = ConflictReport(file)
    from app.model import Entity
    for rec in records:
        # issue #68：键存在值为 None 时 .get 的 default 不生效 → 显式 or 回退
        ent_name = rec.get("entity_name") or rec.get("holder")
        ent = _resolve_entity_id(session, ent_name)
        if ent is None:
            rep.add("H5-引用", rec, f"entity 不存在: {ent_name}")
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


def check_bank_import_conflict(session: Session, file: str, segments: list[dict]) -> ConflictReport:
    """银行台账导入前冲突（issue #15）：H5 引用 + H4 余额断链。

    - H5：segment 持有人映射 entity 不存在 → 拦
    - H4：segment 命中已存在 account 且首笔余额 ≠ 既有末余额 → 拦
    新建账户（无既有余额）不构成 H4 断链。返回 ConflictReport 供上层原样输出明细。
    """
    from app.model import Entity
    rep = ConflictReport(file)
    for seg in segments:
        holder = seg.get("holder")
        cur = seg.get("currency")
        bank = seg.get("bank")
        rows = seg.get("rows") or []
        if not holder or not rows:
            continue                                  # 缺持有人/无流水的 segment 归 writer 跳过，非阻断
        ent = _resolve_entity_id(session, holder)
        if ent is None:
            rep.add("H5-引用", seg.get("seg_title") or holder, f"entity 不存在: {holder}")
            continue
        if not cur:
            continue
        acc = session.execute(
            select(Account).where(Account.entity_id == ent,
                                  Account.currency == cur, Account.bank == bank)
        ).scalar_one_or_none()
        if acc is None:
            continue                                  # 新账户 → H4 无从断链
        new_entries = [{"date": r.get("date"), "balance": r.get("balance")}
                       for r in rows if r.get("balance") is not None]
        if not new_entries:
            continue
        sub = check_account_balance_conflict(session, file, acc.id, new_entries)
        rep.problems.extend(sub.problems)
    return rep


# —— 权威汇率文件识别 ——
AUTHORITY_FX_FILES = ("所有的货币兑换美金.md",)


def is_authority_fx(file: str) -> bool:
    """判断某文件是否权威全量汇率表（DESIGN：全量文件为权威基准）。"""
    return any(f in file for f in AUTHORITY_FX_FILES)


def check_fx_authority_conflict(session: Session, file: str, records: list[dict]) -> ConflictReport:
    """其它汇率文件导入前：同一 (fx_from, fx_to, year) 若权威表已有且值不同 → 冲突(hard-block)。

    权威值 = 权威文件已入库的 exchange_rate；非权威文件记录与之冲突时拦。
    """
    from app.model import ExchangeRate
    rep = ConflictReport(file)
    if is_authority_fx(file):
        return rep                                    # 权威文件自身不检（它是基准）
    for rec in records:
        f, t, y = rec.get("fx_from"), rec.get("fx_to"), rec.get("year")
        if not (f and t):
            continue
        # 查权威值（source 来自权威文件——这里用 exchange_rate 里已存在的那一行；权威已入库）
        auth = session.execute(
            select(ExchangeRate.rate).where(
                ExchangeRate.fx_from == f, ExchangeRate.fx_to == t, ExchangeRate.year == y)
        ).scalar_one_or_none()
        if auth is not None and abs(float(auth) - float(rec["rate"])) > 0.005:
            rep.add("H2-汇率权威", y, f"{f}→{t} {y} 权威 {auth} ≠ 本文件 {rec['rate']}")
    return rep


def ensure_entity(session: Session, name: str, entity_type: str = "person") -> int | None:
    """确保 entity 存在，返回 id；不存在返回 None（供 H5 断链检查）。"""
    from app.model import Entity
    ent = session.execute(
        select(Entity.id).where(Entity.name == name, Entity.entity_type == entity_type)
    ).scalar_one_or_none()
    return int(ent) if ent is not None else None