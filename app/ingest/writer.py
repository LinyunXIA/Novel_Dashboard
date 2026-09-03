"""落库写入（F-P0-04 初始资产摄入 / 基础 entity/account）。

把 parse 结果 + 初始资产写入 DB：
- character -> entity（upsert by entity_type+name）
- initial_asset -> entity + account(by 币种) + initial_asset 表；现金 -> ledger 首笔余额
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.ingest.holders import holder_entity_name
from app.ingest.normalize import resolve_date
from app.model import (Account, Entity, ExchangeRate, FinanceEntry, IncomeStream,
                       InitialAsset, LedgerEntry, Relationship, StockEvent)


def _canonical_person(session: Session, holder: str) -> Entity:
    """职称/登记名 → 规范 person entity（issue #136）。

    与 conflict._resolve_entity_id 同源消费 holders.TITLE_ENTITY 归一，
    杜绝「收益挂账锚（职称别名实体）≠ 银行账户锚（规范实体）」的账务分裂；
    未知名字（不在映射表）原样落档，交由 conflict H2/H5 与 ingest_report 兜底。
    """
    return upsert_entity(session, "person", holder_entity_name(holder) or holder)


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


def upsert_relationship(session: Session, from_entity_id: int, to_entity_id: int,
                        rel_type: str, source_file: str | None = None,
                        since_year: int | None = None,
                        until_year: int | None = None) -> Relationship | None:
    """按 (from, to, rel_type) 幂等 upsert Relationship；同 (from, to, rel_type) 已存在 → 复用。

    issue #27：character 关系字段解析后必须持久化进 relationship 表，P1 人物图谱前置。
    since_year/until_year：可选，覆盖新建/已存在关系的生效年窗（外部 API① 公司持股用；
    保持同键自环跳过语义）。返回 None 表示 from==to（自环）跳过。
    """
    if from_entity_id == to_entity_id:
        return None
    exists = session.execute(
        select(Relationship).where(
            Relationship.from_entity_id == from_entity_id,
            Relationship.to_entity_id == to_entity_id,
            Relationship.rel_type == rel_type,
        ).limit(1)
    ).scalar_one_or_none()
    if exists is not None:
        if since_year is not None:
            exists.since_year = since_year
        if until_year is not None:
            exists.until_year = until_year
        return exists
    rel = Relationship(
        from_entity_id=from_entity_id, to_entity_id=to_entity_id,
        rel_type=rel_type, source_file=source_file,
        since_year=since_year, until_year=until_year,
    )
    session.add(rel)
    session.flush()
    return rel


def get_or_create_account(session: Session, entity_id: int, currency: str,
                          bank: str | None = None) -> Account:
    """按唯一键 (entity, currency, bank) 取/建账户（issue #68：bank 参与匹配，
    避免同主体同币种多开户行时 scalar_one_or_none 命中多行）。"""
    acc = session.execute(
        select(Account).where(Account.entity_id == entity_id,
                              Account.currency == currency,
                              Account.bank == bank)
    ).scalars().first()
    if acc is None:
        acc = Account(entity_id=entity_id, currency=currency, bank=bank)
        session.add(acc)
        session.flush()
    return acc


def import_characters(session: Session, records: list[dict], source_file: str | None = None) -> dict:
    """character 解析记录 → entity + relationship（按 rels 中 target 名查 entity upsert）。

    issue #27 修复：
    - relations 形如 [("与主角的关系", "养父"), ("关系", "管家张三"), ...]
    - target_name 查 entity（按 entity_type='person', name=target_name）；失配 → warnings
    - 关系 (from, to, rel_type) 幂等 upsert；自环（from==to）跳过

    返回 {"imported": entity 数, "rels": 关系数, "warnings": [target 未找到的列表]}
    """
    stats: dict = {"imported": 0, "rels": 0, "warnings": []}
    for rec in records:
        ent = upsert_entity(session, "person", rec["name"], display_name=rec.get("display_name"),
                            fields=rec.get("fields"),
                            source_file=rec.get("source_file") or source_file)
        stats["imported"] += 1
        for rel_key, rel_val in rec.get("relations") or []:
            target_name = rel_val.split("/")[0].strip() if rel_val else ""
            if not target_name or target_name == rec["name"]:
                continue  # 空值或自环
            to = session.execute(
                select(Entity).where(Entity.entity_type == "person", Entity.name == target_name)
            ).scalar_one_or_none()
            if to is None:
                stats["warnings"].append(f"{rec['name']} → {target_name}（{rel_key}）目标未注册")
                continue
            upsert_relationship(session, ent.id, to.id, rel_key,
                                source_file=rec.get("source_file") or source_file)
            stats["rels"] += 1
    return stats


def import_initial_assets(session: Session, records: list[dict], cash_year: int = 1947) -> dict:
    """初始资产 → entity/inital_asset/account；现金进余额(ledger 首笔)。

    issue #68：自然键去重兜底——现金按 (account, reason='初始现金', inflow)、
    存量按 (entity, asset_type, name, group_key) 已存在则跳过；
    兼容早于 source_file_version 机制导入的存量库重复 ingest 场景。

    返回统计。
    """
    stats = {"asset": 0, "cash": 0, "cash_skipped": 0, "asset_skipped": 0}
    for rec in records:
        ent = _canonical_person(session, rec["entity_name"])
        if rec["asset_type"] == "cash":
            # 现金 → 账户 + 首笔存款
            cur = rec["currency"]
            acc = get_or_create_account(session, ent.id, cur)
            amort = rec["face_value"]
            dup = session.execute(
                select(LedgerEntry.id).where(
                    LedgerEntry.account_id == acc.id,
                    LedgerEntry.reason == "初始现金",
                    LedgerEntry.inflow == amort,
                ).limit(1)
            ).scalar_one_or_none()
            if dup is not None:
                stats["cash_skipped"] += 1
                continue
            session.add(LedgerEntry(account_id=acc.id, date=resolve_date(cash_year),
                                    reason="初始现金", inflow=amort, balance=amort,
                                    kind="income", source_file=rec.get("source_file")))
            stats["cash"] += 1
        else:
            dup = session.execute(
                select(InitialAsset.id).where(
                    InitialAsset.entity_id == ent.id,
                    InitialAsset.asset_type == rec["asset_type"],
                    InitialAsset.name == rec.get("name"),
                    InitialAsset.group_key == rec.get("group_key"),
                ).limit(1)
            ).scalar_one_or_none()
            if dup is not None:
                stats["asset_skipped"] += 1
                continue
            session.add(InitialAsset(
                entity_id=ent.id, asset_type=rec["asset_type"],
                group_key=rec.get("group_key"), currency=rec.get("currency"),
                name=rec.get("name"), face_value=rec.get("face_value"),
                pct=rec.get("pct"), source_file=rec.get("source_file"),
            ))
            stats["asset"] += 1
    return stats


def _mirror_to_finance(session: Session, *, entity: Entity, cur: str, year: int,
                       amount, label: str, source_file: str | None = None,
                       kind: str = "income") -> None:
    """issue #80：把 ingest 的 income_stream / 家庭支出镜像为 finance_entry(source='file')，
    使财务收支屏（F-P1-07）在真实库可见。entity_kind 从 entity.entity_type 推（person/company）。"""
    if entity.entity_type not in ("person", "company"):
        return
    session.add(FinanceEntry(
        entity_id=entity.id, entity_kind=entity.entity_type, year=year,
        kind=kind, amount=amount, currency=cur, label=label,
        source="file", source_file=source_file,
    ))


def import_basic_income(session: Session, records: list[dict]) -> dict:
    """基本收入.md → 逐年 income_stream（issue #211，逐年终值直入）。

    整合取代旧四类配置推导链路（security 票息展开 / rent·property 分段复利 /
    shop 时段均值）：parser 已按年份段展开为逐年记录，writer 只做归属归一 +
    落库 + finance_entry 镜像。记录自带 stream_type/group_key/label/currency/
    year/amount/source_line；holder 经 TITLE_ENTITY 归一挂规范实体（issue #136）。
    0 值由 parser 跳过；返回 {stream: 生成行}。
    """
    stats = {"stream": 0}
    for rec in records:
        holder = rec.get("holder")
        amount = rec.get("amount")
        if not holder or not amount:
            continue
        ent = _canonical_person(session, holder)
        session.add(IncomeStream(
            entity_id=ent.id, stream_type=rec["stream_type"], group_key=rec["group_key"],
            currency=rec.get("currency"), year=rec["year"], amount=amount,
            label=rec.get("label"), source_file=rec.get("source_file"),
            source_line=rec.get("source_line"),
        ))
        _mirror_to_finance(session, entity=ent, cur=rec.get("currency"),
                           year=rec["year"], amount=amount, label=rec.get("label"),
                           source_file=rec.get("source_file"))
        stats["stream"] += 1
    return stats


def import_fx(session: Session, records: list[dict], update: bool = False) -> dict:
    """汇率 → exchange_rate。

    幂等：按 (fx_from,fx_to,year) 已存在则跳过（insert-only，默认）。
    issue #116：update=True（权威全量表内容变更重导）时改为 upsert——
    同键 rate 不同 → 覆盖更新并计数；缺失 → 插入。非权威文件仍走冲突检测+insert-only。
    """
    from app.model import ExchangeRate
    stats = {"n": 0, "updated": 0}
    if not records:
        return stats
    for rec in records:
        row = session.execute(
            select(ExchangeRate).where(
                ExchangeRate.fx_from == rec["fx_from"], ExchangeRate.fx_to == rec["fx_to"],
                ExchangeRate.year == rec.get("year"))
        ).scalar_one_or_none()
        if row is None:
            session.add(ExchangeRate(fx_from=rec["fx_from"], fx_to=rec["fx_to"],
                                     year=rec.get("year"), rate=rec["rate"]))
            stats["n"] += 1
        elif update and row.rate is not None and Decimal(str(row.rate)) != Decimal(str(rec["rate"])):
            row.rate = rec["rate"]
            stats["updated"] += 1
    return stats


def import_return_curves(session: Session, records: list[dict]) -> dict:
    """收益测算表 → return_curve（upsert，幂等）。

    issue #214：由 ON CONFLICT DO NOTHING 改为同键更新——全球五地整合文件
    取代 5 张分地区表时，1050 行数值不变、仅 source_file 溯源需刷新；且未来
    史实数值修订重跑 ingest 即可落地（DO NOTHING 下同键新值永远进不了库）。
    新键走 pg_insert ... ON CONFLICT DO NOTHING，同键 rate/source_file 有变走
    ORM 更新；无变化行不写，第二轮导入 n=0。
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from app.model import ReturnCurve
    stats = {"n": 0, "inserted": 0, "updated": 0}
    if not records:
        return stats
    existing = {(c.country, c.risk_lvl, c.year): c
                for c in session.query(ReturnCurve).all()}
    to_insert: list[dict] = []
    for r in records:
        key = (r["country"], r["risk_lvl"], r["year"])
        source_file = r.get("source_file")
        cur = existing.get(key)
        # rate 列为 Numeric（读回 Decimal），与 record 的 float 比较前先转 float
        cur_rate = float(cur.rate) if cur is not None else None
        if cur is None:
            to_insert.append({"country": r["country"], "risk_lvl": r["risk_lvl"],
                              "year": r["year"], "rate": r["rate"],
                              "source_file": source_file})
        elif cur_rate != float(r["rate"]) or cur.source_file != source_file:
            cur.rate = r["rate"]
            cur.source_file = source_file
            stats["updated"] += 1
    if to_insert:
        stmt = pg_insert(ReturnCurve).values(to_insert).on_conflict_do_nothing(
            constraint="uq_return_country_risk_year"
        ).returning(ReturnCurve.id)
        stats["inserted"] = len(session.execute(stmt).scalars().all())
    stats["n"] = stats["inserted"] + stats["updated"]
    return stats


def import_timeline(session: Session, records: list[dict]) -> dict:
    """时间线事件 → timeline_event。

    幂等键：同 (event_year, title, source_file) 已存在 → 跳过；否则插入。
    用于 H1 时间线对齐 / H1 时间线校验 / 日历游标。
    """
    from app.model import TimelineEvent
    stats = {"n": 0, "skipped": 0}
    if not records:
        return stats
    for rec in records:
        title = (rec.get("title") or "").strip()
        if not title:
            continue
        year = rec.get("event_year")
        if year is None:
            continue
        exists = session.execute(
            select(TimelineEvent.id).where(
                TimelineEvent.event_year == year,
                TimelineEvent.title == title,
                TimelineEvent.source_file == rec.get("source_file"),
            ).limit(1)
        ).scalar_one_or_none()
        if exists is not None:
            stats["skipped"] += 1
            continue
        session.add(TimelineEvent(
            event_year=year,
            event_date=rec.get("event_date"),
            title=title,
            note=rec.get("note"),
            decade=rec.get("decade"),
            source_file=rec.get("source_file"),
        ))
        stats["n"] += 1
    return stats


def import_salary(session: Session, records: list[dict]) -> dict:
    """薪资 → 逐年 income_stream(salary)，各归各（养父→Joren/养母→Johanna），取文件税后值。

    issue #136：holder 经 TITLE_ENTITY 归一挂规范实体；group_key/label 同用规范名，
    保证与账户锚同源。

    issue #220：改**按人替换式**——薪资文件是某人入职至退休的逐年全量台账（salary 流
    的唯一写入方），导入前先删该 entity 既有 salary income_stream 及 finance_entry
    镜像（不限 source_file），再批量插入。文件名更替（养父的薪资.md →
    养父的薪资_CNY修正版.md）或口径修正（USD→CNY）时，老行随替换自然清场，
    不会出现同年 BEF/USD/CNY 双份薪资；二跑行数恒定（幂等）。

    issue #222：文件表外「退职金专项核算」段产出 component='severance' 记录
    （退休年一次性税后退职金，比利时 Assigned out 口径、EUR），并入 salary 流、
    group_key/label 用「退职金」与逐年薪资区分；替换清场同时覆盖两类镜像。
    """
    from app.model.types import StreamType
    stats = {"stream": 0, "replaced": 0}

    def _labels(ent: Entity) -> tuple[str, str]:
        return f"{ent.name}薪资税后", f"{ent.name}退职金税后"

    # 归一 holder → entity（去重），再按 entity 清场旧 salary 行
    ents: dict[str, Entity] = {}
    for rec in records:
        holder = rec.get("holder")
        if holder and holder not in ents:
            ents[holder] = _canonical_person(session, rec["holder"])
    for ent in ents.values():
        sal_label, sev_label = _labels(ent)
        old_streams = session.execute(
            select(IncomeStream).where(
                IncomeStream.entity_id == ent.id,
                IncomeStream.stream_type == StreamType.SALARY.value)
        ).scalars().all()
        for row in old_streams:
            session.delete(row)
            stats["replaced"] += 1
        old_mirror = session.execute(
            select(FinanceEntry).where(
                FinanceEntry.entity_id == ent.id,
                FinanceEntry.kind == "income",
                or_(FinanceEntry.label == sal_label, FinanceEntry.label == sev_label))
        ).scalars().all()
        for row in old_mirror:
            session.delete(row)
    session.flush()
    for rec in records:
        ent = ents.get(rec.get("holder"))
        if ent is None or rec.get("after_tax") is None:
            continue
        if rec.get("component") == "severance":
            group_key, label = f"{ent.name}退职金", f"{ent.name}退职金税后"
        else:
            group_key, label = f"{ent.name}薪资", f"{ent.name}薪资税后"
        session.add(IncomeStream(
            entity_id=ent.id, stream_type=StreamType.SALARY.value, group_key=group_key,
            currency=rec.get("currency"), year=rec["year"], amount=rec["after_tax"],
            label=label, source_file=rec.get("source_file"),
        ))
        _mirror_to_finance(session, entity=ent, cur=rec.get("currency"),
                           year=rec["year"], amount=rec["after_tax"],
                           label=label, source_file=rec.get("source_file"))
        stats["stream"] += 1
    return stats


def import_household_expense(session: Session, records: list[dict]) -> dict:
    """家庭支出 → 逐年 ledger 支出（挂 Henri Peeters 账户，多年一致 BEF）。

    issue #68：自然键去重兜底——(account, date, reason='家庭支出') 已存在则跳过；
    兼容早于 source_file_version 机制导入的存量库重复 ingest 场景。
    issue #216：改 upsert——同键行金额/来源有变则更新 ledger 并同步 finance_entry
    镜像（修正版文件替换或未来金额修订时，重跑 ingest 即落地；旧 DO NOTHING 语义
    下金额修订会同年插入第二笔支出，重复计账）。无变化不写，第二轮 n=0。
    """
    stats = {"n": 0, "skipped": 0, "updated": 0}
    for rec in records:
        ent = _canonical_person(session, rec["holder"])
        cur = rec.get("currency") or "BEF"
        acc = get_or_create_account(session, ent.id, cur)
        d = resolve_date(rec["year"])
        source_file = rec.get("source_file")
        existing = session.execute(
            select(LedgerEntry).where(
                LedgerEntry.account_id == acc.id,
                LedgerEntry.date == d,
                LedgerEntry.reason == "家庭支出",
            ).limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            cur_amount = float(existing.outflow) if existing.outflow is not None else None
            if cur_amount == float(rec["amount"]) and existing.source_file == source_file:
                stats["skipped"] += 1
                continue
            existing.outflow = rec["amount"]
            existing.source_file = source_file
            # 同步 finance_entry 镜像（entity/year/label/kind 定位；缺失则补）
            mirror = session.execute(
                select(FinanceEntry).where(
                    FinanceEntry.entity_id == ent.id,
                    FinanceEntry.year == d.year,
                    FinanceEntry.kind == "expense",
                    FinanceEntry.label == "家庭支出",
                    FinanceEntry.source == "file",
                ).limit(1)
            ).scalar_one_or_none()
            if mirror is not None:
                mirror.amount = rec["amount"]
                mirror.source_file = source_file
            else:
                _mirror_to_finance(session, entity=ent, cur=cur, year=d.year,
                                   amount=rec["amount"], label="家庭支出",
                                   source_file=source_file, kind="expense")
            stats["updated"] += 1
            continue
        session.add(LedgerEntry(
            account_id=acc.id, date=d, reason="家庭支出",
            outflow=rec["amount"], balance=None, kind="expense",
            source_file=source_file,
        ))
        _mirror_to_finance(session, entity=ent, cur=cur, year=d.year,
                           amount=rec["amount"], label="家庭支出",
                           source_file=source_file, kind="expense")
        stats["n"] += 1
    return stats


def _ledger_kind_from_reason(reason: str, *, is_inflow: bool) -> str:
    """从「理由」文本推断 ledger kind（DESIGN §5.2 ledger_entry.kind）。"""
    r = (reason or "").strip()
    if not r:
        return "income" if is_inflow else "expense"
    # 投资类收益 → investment_income
    if any(k in r for k in ("证券收入", "票息", "分红", "杠杆投资", "净值", "投资收入")):
        return "investment_income"
    # 划拨/注资/转入 → 仍是 income
    if any(k in r for k in ("划拨", "注资", "转入", "转入", "增资")):
        return "income"
    # 支出/费用 → expense
    if any(k in r for k in ("支出", "费用", "成本", "用工", "外包")):
        return "expense"
    return "income" if is_inflow else "expense"


def import_bank(session: Session, segments: list[dict], source_file: str | None = None) -> dict:
    """银行台账：每个 segment 一个 account(entity,currency,bank)，逐行写 ledger_entry。

    - 缺 entity 归属（holder=None）→ 该 segment 跳过并计入 skipped；不静默丢
    - 缺 currency → 该 segment 跳过
    - 同行 inflow/outflow 都为 None → 跳过该行
    - 余额缺失 → 存 None（重算时按 inflow-outflow 推进）

    返回 {ledger: 行数, account: 账户数, skipped: 跳过 segment 数}
    """
    stats: dict[str, int] = {"ledger": 0, "account": 0, "skipped": 0}
    for seg in segments:
        holder = seg.get("holder")
        cur = seg.get("currency")
        bank = seg.get("bank")
        rows = seg.get("rows") or []
        if not holder:
            stats["skipped"] += 1
            continue
        if not cur:
            stats["skipped"] += 1
            continue
        # entity + account（唯一键：entity × currency × bank）；issue #136：holder 归一规范名
        ent = _canonical_person(session, holder)
        acc = session.execute(
            select(Account).where(
                Account.entity_id == ent.id, Account.currency == cur, Account.bank == bank)
        ).scalar_one_or_none()
        if acc is None:
            acc = Account(entity_id=ent.id, currency=cur, bank=bank)
            session.add(acc)
            session.flush()
            stats["account"] += 1
        for row in rows:
            inflow = row.get("inflow")
            outflow = row.get("outflow")
            if inflow is None and outflow is None:
                continue
            dt = row.get("date")
            if isinstance(dt, date):
                d = dt  # parse_bank 已用 parse_date_cell 归一为 date（issue #19）
            else:
                try:
                    d = datetime.fromisoformat((dt or "").replace("/", "-").replace("－", "-")).date()
                except (TypeError, ValueError):
                    continue
            kind = _ledger_kind_from_reason(row.get("reason", ""),
                                            is_inflow=(inflow or 0) > 0)
            session.add(LedgerEntry(
                account_id=acc.id, date=d, reason=row.get("reason"),
                inflow=inflow, outflow=outflow, balance=row.get("balance"),
                kind=kind, note=row.get("note"),
                source_file=source_file,
            ))
            stats["ledger"] += 1
    return stats


# EMU 锁定不变折算率：EUR=X → balance/rate（DESIGN §6.6；BEF 明文，LUF 与 BEF 平比价，NLG 官方锁定）
_EUR_CONVERSION = {"BEF": 40.3399, "LUF": 40.3399, "NLG": 2.20371}


def close_2002_currency(session: Session, currencies=("BEF", "LUF", "NLG"),
                        closed_on="2002-01-01", migrate_to="EUR") -> dict:
    """2002-01-01 关闭 BEF/LUF/NLG 池 → migrate_to EUR，并开 EUR 承接分录（DESIGN §6.6）。

    按 entity 聚合：同 entity 的所有被关旧币账户（可多币种/多账户）合并计入该 entity
    的**一条** EUR 承接分录（DESIGN §6.6「开一条承接分录」）。对每个 entity：
    1. 置其全部 active 旧币账户为 closed（closed_on/migrate_to_currency）。
    2. 开/复用同 entity 的 EUR 池（存在多个 EUR 池时归入首个，避免 multi-result）。
    3. 每条旧币账户取关池日余额 = Σ(inflow) − Σ(outflow) for date ≤ 2002-01-01
       （不依赖 ledger.balance——重算链可能尚未跑，source/bank 余额不可靠）；余额为 0 计入
       skipped_zero、不结转。
    4. 各币种折算后汇总为 EUR，写一条承接 inflow（note 记各原币金额与折算汇率；balance=inflow 作 EUR 池起点）。

    幂等：
    - 外层按 status='active' 过滤 → 二次运行不再重复关池/承接；
    - 写前查 EUR 池是否已有 2002-01-01「关池划转」入账，有则跳过（按 entity 一条，天然防重）。

    返回 {"closed": 关池账户数, "migrated": 承接分录(entity)数, "skipped_zero": 零余额未结转账户数}。
    """
    from collections import defaultdict
    from datetime import date as _date
    c = _date(2002, 1, 1)
    accs = session.execute(
        select(Account).where(Account.currency.in_(currencies), Account.status == "active")
    ).scalars().all()
    by_entity: dict[int, list[Account]] = defaultdict(list)
    for a in accs:
        by_entity[a.entity_id].append(a)
    stats = {"closed": 0, "migrated": 0, "skipped_zero": 0}
    for eid, alist in by_entity.items():
        for a in alist:
            a.status = "closed"
            a.closed_on = c
            a.migrate_to_currency = migrate_to
        stats["closed"] += len(alist)
        # EUR 承接池：取同 entity 任一 EUR 账户；无则新建（bank=None 统一池）
        eur_acc = session.execute(
            select(Account).where(Account.entity_id == eid, Account.currency == migrate_to)
        ).scalars().first()
        if eur_acc is None:
            eur_acc = Account(entity_id=eid, currency=migrate_to, bank=None)
            session.add(eur_acc)
            session.flush()
        # 幂等防重：EUR 池已有关池日承接入账 → 跳过（按 entity 一条）
        already = session.execute(
            select(LedgerEntry.id).where(
                LedgerEntry.account_id == eur_acc.id, LedgerEntry.date == c,
                LedgerEntry.reason.like("%关池划转%")).limit(1)
        ).scalar_one_or_none()
        if already is not None:
            continue
        # 各旧币账户折算汇总
        parts: list[str] = []
        total_eur = 0.0
        for a in alist:
            rate = _EUR_CONVERSION.get(a.currency)
            if rate is None:
                continue
            tin, tout = session.execute(
                select(func.coalesce(func.sum(LedgerEntry.inflow), 0),
                       func.coalesce(func.sum(LedgerEntry.outflow), 0))
                .where(LedgerEntry.account_id == a.id, LedgerEntry.date <= c)
            ).one()
            legacy = float(tin) - float(tout)
            if legacy == 0:
                stats["skipped_zero"] += 1
                continue
            total_eur += legacy / rate
            parts.append(f"{a.currency} {legacy:,.2f}（折算汇率 1 {migrate_to} = {rate} {a.currency}）")
        if not parts:
            continue
        amount = round(total_eur, 2)
        note = "承接自 " + "；".join(parts)
        session.add(LedgerEntry(
            account_id=eur_acc.id, date=c, reason=f"2002关池划转 ({'/'.join(currencies)}→{migrate_to})",
            inflow=amount, outflow=None, balance=amount, kind="income", note=note,
        ))
        stats["migrated"] += 1
    return stats


def backfill_finance_entries(session: Session) -> dict:
    """F-P1-07 真实库回填：把 issue #80 之前已导入的既有 income_stream / 家庭支出镜像到 finance_entry。

    现有数据早于 _mirror_to_finance，重浇灌幂等跳过 → finance_entry 为空；此回填补上财务收支屏数据源。
    幂等：按 (entity_id, year, kind, label, amount) 存在则跳过。返回统计。
    """
    stats = {"income": 0, "expense": 0, "skipped_income": 0, "skipped_expense": 0,
             "unresolved_entity": 0}
    bis = session.execute(select(Entity)).scalars().all()
    ents = {e.id: e for e in bis}
    # income_stream → finance_entry(income)
    for s in session.execute(select(IncomeStream)).scalars().all():
        e = ents.get(s.entity_id)
        if e is None or e.entity_type not in ("person", "company"):
            stats["unresolved_entity"] += 1
            continue
        label = s.label or ""
        dup = session.execute(select(FinanceEntry.id).where(
            FinanceEntry.entity_id == s.entity_id, FinanceEntry.year == s.year,
            FinanceEntry.kind == "income", FinanceEntry.amount == s.amount,
            FinanceEntry.currency == s.currency,
            FinanceEntry.label == label).limit(1)).scalar_one_or_none()
        if dup is not None:
            stats["skipped_income"] += 1
            continue
        session.add(FinanceEntry(entity_id=s.entity_id, entity_kind=e.entity_type,
                                 year=s.year, kind="income", amount=s.amount,
                                 currency=s.currency, label=label, source="file"))
        stats["income"] += 1
    # ledger 家庭支出 → finance_entry(expense)
    expense_rows = session.execute(
        select(LedgerEntry).where(LedgerEntry.reason == "家庭支出")).scalars().all()
    for le in expense_rows:
        acc = session.get(Account, le.account_id)
        e = ents.get(acc.entity_id) if acc else None
        if e is None:
            stats["unresolved_entity"] += 1
            continue
        dup = session.execute(select(FinanceEntry.id).where(
            FinanceEntry.entity_id == e.id, FinanceEntry.year == le.date.year,
            FinanceEntry.kind == "expense", FinanceEntry.amount == le.outflow,
            FinanceEntry.label == "家庭支出").limit(1)).scalar_one_or_none()
        if dup is not None:
            stats["skipped_expense"] += 1
            continue
        session.add(FinanceEntry(entity_id=e.id, entity_kind=e.entity_type,
                                 year=le.date.year, kind="expense", amount=le.outflow,
                                 currency=acc.currency if acc else None,
                                 label="家庭支出", source="file"))
        stats["expense"] += 1
    return stats

def import_movie_events(session: Session, records: list[dict]) -> dict:
    """F-P2-01：电影事件落库（幂等 upsert by title+source_file）。"""
    from app.model.movie_event import MovieEvent
    stats = {"inserted": 0, "skipped": 0}
    for r in records:
        dup = session.execute(select(MovieEvent.id).where(
            MovieEvent.title == r["title"],
            MovieEvent.source_file == r["source_file"]).limit(1)).scalar_one_or_none()
        if dup is not None:
            stats["skipped"] += 1
            continue
        session.add(MovieEvent(**{k: v for k, v in r.items()}))
        stats["inserted"] += 1
    return stats


def import_stock_events(session: Session, records: list[dict]) -> dict:
    """F-P2-02：股票事件落库（幂等 upsert by company+date+event_type+source_file）。

    只落**待关联**记录（StockEvent），不写 holding_event/ledger——account/entity 由 UI 同币种
    手动关联时解析（block D associate → apply_buy 等）。currency 默认 USD（阶段一仅 USD）。
    """
    stats = {"inserted": 0, "skipped": 0}
    seen: set[tuple] = set()   # 批内去重（best-effort 解析同一文件可能产出多条 (company,date,event_type) 相同的杂行）
    for r in records:
        d = r["date"] if isinstance(r["date"], date) else date.fromisoformat(str(r["date"]))
        key = (r["company"], d, r["event_type"], r["source_file"])
        if key in seen:
            stats["skipped"] += 1
            continue
        seen.add(key)
        dup = session.execute(select(StockEvent.id).where(
            StockEvent.company == key[0], StockEvent.date == key[1],
            StockEvent.event_type == key[2], StockEvent.source_file == key[3],
        ).limit(1)).scalar_one_or_none()
        if dup is not None:
            stats["skipped"] += 1
            continue
        payload = {k: v for k, v in r.items()}
        payload.setdefault("currency", "USD")
        payload["date"] = d
        session.add(StockEvent(**payload))
        stats["inserted"] += 1
    return stats
