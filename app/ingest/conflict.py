"""导入前冲突检测 hard-block（DESIGN §11.4）。

在写库前比对 新记录 vs DB 既有相关记录。严重度两级（issue #72 对齐 §11.4）：

**挡（hard-block，problems）**——该文件不入库：
- 金额冲突(H2)：同 entity×stream_type×currency×year 已有不同金额
- 余额断链(H4)：账户追加流水时，新首笔前值 ≠ 已存在末余额
- 汇率闭合(H3)：新汇率使 A→B→C ≠ A→C（>0.5%）

**标（soft warning，warnings）**——入库但在 ingest_report 高亮：
- 时间线对齐(H1)：收益年份无任何编年史条目（增量瘦版）
- 断链/引用(H5)：回引不存在的 entity

命中冲突由上层将整文件标为"需人工"、不入库；软警告随导入输出。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.model import Account, HoldingEvent, IncomeStream, LedgerEntry, StockEvent


class ConflictError(Exception):
    def __init__(self, file: str, rule: str, detail: str):
        super().__init__(f"[{rule}] {file}: {detail}")
        self.file = file
        self.rule = rule
        self.detail = detail


@dataclass
class ConflictReport:
    """单文件冲突检测报告。

    - problems：硬冲突（§11.4「挡」），任一命中 → 整文件不入库
    - warnings：软警告（§11.4「标」），入库但需在报告中高亮
    """
    file: str
    problems: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.problems)

    def add(self, rule: str, line: object, desc: str):
        self.problems.append({"rule": rule, "line": line, "detail": desc})

    def add_warning(self, rule: str, line: object, desc: str):
        self.warnings.append({"rule": rule, "line": line, "detail": desc})

    def merge(self, other: "ConflictReport") -> None:
        self.problems.extend(other.problems)
        self.warnings.extend(other.warnings)


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
    for rec in records:
        # issue #68：键存在值为 None 时 .get 的 default 不生效 → 显式 or 回退
        ent_name = rec.get("entity_name") or rec.get("holder")
        ent = _resolve_entity_id(session, ent_name)
        if ent is None:
            # issue #72：H5 引用失配按 §11.4 定级为「标」（soft）
            rep.add_warning("H5-引用", rec, f"entity 不存在: {ent_name}")
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


def check_timeline_alignment(session: Session, file: str, records: list[dict]) -> ConflictReport:
    """H1 增量瘦版（issue #72）：新收益年份在 timeline_event 无任何条目 → 标。

    §11.4「时间线对齐」的导入侧实现：不比对具体事件，只查年份覆盖盲区；
    全量跨文件 JOIN 仍归 health.py（导入后汇总视图）。
    """
    rep = ConflictReport(file)
    from app.model import TimelineEvent
    years = sorted({int(r["year"]) for r in records if r.get("year") is not None})
    if not years:
        return rep
    covered = set(session.execute(
        select(TimelineEvent.event_year).where(TimelineEvent.event_year.in_(years))
    ).scalars().all())
    for y in years:
        if y not in covered:
            rep.add_warning("H1-时间线", y, f"{y} 无任何时间线事件（可能缺编年史条目）")
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

    - H5：segment 持有人映射 entity 不存在 → 标（issue #72：soft，§11.4 定级）
    - H4：segment 命中已存在 account 且首笔余额 ≠ 既有末余额 → 拦
    新建账户（无既有余额）不构成 H4 断链。返回 ConflictReport 供上层原样输出明细。
    """
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
            rep.add_warning("H5-引用", seg.get("seg_title") or holder,
                            f"entity 不存在: {holder}")
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


def _rate_map(session: Session, staged: list[dict]) -> dict[tuple[str, str], dict]:
    """汇率视图：DB ∪ 本批暂存（暂存优先），键 (from,to) → {year_or_0: rate}。"""
    from app.model import ExchangeRate
    out: dict[tuple[str, str], dict] = {}
    for r in session.execute(select(ExchangeRate)).scalars().all():
        out.setdefault((r.fx_from, r.fx_to), {})[r.year or 0] = float(r.rate)
    for rec in staged:
        f, t, y = rec.get("fx_from"), rec.get("fx_to"), rec.get("year")
        if f and t and rec.get("rate") is not None:
            out.setdefault((f, t), {})[y or 0] = float(rec["rate"])
    return out


def check_fx_chain_closure(session: Session, file: str, records: list[dict]) -> ConflictReport:
    """H3 链式闭合增量预检（issue #72）：新汇率参与下 A→B→C ≠ A→C（>0.5%）→ 挡。

    视图 = DB 现有汇率 ∪ 本批暂存；仅评估两跳链 vs 直接汇率，
    全量闭合校验仍归 health.check_h3_fx_closure（导入后）。
    """
    rep = ConflictReport(file)
    if not records:
        return rep
    pairs = _rate_map(session, records)

    def _val(f: str, t: str, y: int | None):
        d = pairs.get((f, t))
        if not d:
            return None
        return d.get(y or 0, d.get(0))

    seen: set[tuple] = set()
    for rec in records:
        a, b, y = rec.get("fx_from"), rec.get("fx_to"), rec.get("year")
        if not (a and b):
            continue
        direct = _val(a, b, y)
        if direct is None or direct == 0:
            continue
        # 遍历所有 a→x→b 两跳链
        for (p, q), _ in pairs.items():
            if p != a or q == b or q == a:
                continue
            v1 = _val(p, q, y)
            v2 = _val(q, b, y)
            if v1 is None or v2 is None or v1 * v2 == 0:
                continue
            key = (a, b, q, y)
            if key in seen:
                continue
            seen.add(key)
            if abs(v1 * v2 - direct) / abs(direct) > 0.005:
                rep.add("H3-汇率闭合", y,
                        f"{a}→{q}→{b} 链式 {v1*v2:.4f} ≠ 直接 {direct}（{a}→{b} @{y or '常量'}）")
    return rep


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
        auth = session.execute(
            select(ExchangeRate.rate).where(
                ExchangeRate.fx_from == f, ExchangeRate.fx_to == t, ExchangeRate.year == y)
        ).scalar_one_or_none()
        if auth is not None and abs(float(auth) - float(rec["rate"])) > 0.005:
            rep.add("H2-汇率权威", y, f"{f}→{t} {y} 权威 {auth} ≠ 本文件 {rec['rate']}")
    return rep


def check_stock_event_conflict(session: Session, file: str, records: list[dict]) -> ConflictReport:
    """§11.4 股票事件导入前冲突（F-P2-04）：同 (company, date, event_type) 同键值打架 → hard-block。

    新文件的记录 vs DB 既有 StockEvent / HoldingEvent（同一键但 amount/shares 不同）→ 挡。
    （events_stock 的批内去重由 import_stock_events 的 (company,date,event_type,source_file) 幂等键负责，
    这里只拦**跨文件**/与已实体化链的金额不一致。）
    """
    from datetime import date as _date
    rep = ConflictReport(file)

    def _val(v):
        return float(v) if v is not None else None

    for rec in records:
        comp = rec.get("company")
        et = rec.get("event_type")
        if not comp or not et:
            continue
        rd = rec.get("date")
        d = _date.fromisoformat(rd) if isinstance(rd, str) else rd
        ramt, rsh = _val(rec.get("amount")), _val(rec.get("shares"))
        # 既有 StockEvent（**其他**文件；同一 source_file 的重导入由 import_stock_events 幂等 upsert 处理，不算冲突）
        for se in session.execute(select(StockEvent).where(
                StockEvent.company == comp, StockEvent.date == d,
                StockEvent.event_type == et, StockEvent.source_file != file)).scalars().all():
            if (ramt is not None and se.amount is not None and abs(ramt - float(se.amount)) > 1e-3) or \
               (rsh is not None and se.shares is not None and abs(rsh - float(se.shares)) > 1e-3):
                rep.add("H2-股票", d, f"{comp} {et} {d} 既有 StockEvent(金额 {se.amount}/股 {se.shares}) "
                                     f"≠ 本文件(金额 {ramt}/股 {rsh})")
        # 已实体化的 HoldingEvent（链/手动已建）——仅当记录指定了 entity_id 才可比对
        eid = rec.get("entity_id")
        if eid is not None:
            for h in session.execute(select(HoldingEvent).where(
                    HoldingEvent.entity_id == eid, HoldingEvent.company == comp,
                    HoldingEvent.date == d, HoldingEvent.event_type == et)).scalars().all():
                if (ramt is not None and h.amount is not None and abs(ramt - float(h.amount)) > 1e-3) or \
                   (rsh is not None and h.shares is not None and abs(rsh - float(h.shares)) > 1e-3):
                    rep.add("H2-股票", d, f"{comp} {et} {d} 既有 HoldingEvent(金额 {h.amount}/股 {h.shares}) "
                                         f"≠ 本文件(金额 {ramt}/股 {rsh})")
    return rep


def ensure_entity(session: Session, name: str, entity_type: str = "person") -> int | None:
    """确保 entity 存在，返回 id；不存在返回 None（供 H5 断链检查）。"""
    from app.model import Entity
    ent = session.execute(
        select(Entity.id).where(Entity.name == name, Entity.entity_type == entity_type)
    ).scalar_one_or_none()
    return int(ent) if ent is not None else None
