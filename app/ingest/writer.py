"""落库写入（F-P0-04 初始资产摄入 / 基础 entity/account）。

把 parse 结果 + 初始资产写入 DB：
- character -> entity（upsert by entity_type+name）
- initial_asset -> entity + account(by 币种) + initial_asset 表；现金 -> ledger 首笔余额
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ingest.normalize import resolve_date
from app.model import Account, Entity, ExchangeRate, IncomeStream, InitialAsset, LedgerEntry


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
            session.add(LedgerEntry(account_id=acc.id, date=resolve_date(cash_year),
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


def _property_factor(year: int) -> float:
    """经营性房产逐年复利：1974 基桩=1；1975-84 +7%、85-99 +3.5%、00-07 +5%；08-16 +3%、17-22 +2.8%、23-25 +1.5%。"""
    if year <= 1974:
        return 1.0
    f = 1.0
    for y in range(1975, year + 1):
        if y <= 1984:
            f *= 1.07
        elif y <= 1999:
            f *= 1.035
        elif y <= 2007:
            f *= 1.05
        elif y <= 2016:
            f *= 1.03
        elif y <= 2022:
            f *= 1.028
        else:
            f *= 1.015
    return f


def import_income_property(session: Session, records: list[dict],
                           years: tuple[int, int] = (1974, 2025)) -> dict:
    """经营性房产 → 逐年营收 income_stream（属地基准 × 分段复利；营收口径，不含人力成本—归 P1 用工成本线）。"""
    stats = {"stream": 0}
    for rec in records:
        ent = upsert_entity(session, "person", rec["holder"])
        base = rec.get("base1974") or 0.0
        for y in range(years[0], years[1] + 1):
            if y < 1974:
                continue
            session.add(IncomeStream(
                entity_id=ent.id, stream_type="property", group_key=f"{rec.get('country')}{rec.get('prop')}",
                currency=rec.get("currency"), year=y, amount=round(base * _property_factor(y), 2),
                label=f"经营性房产 · {rec.get('country')}{rec.get('prop')}",
                source_file=rec.get("source_file"),
            ))
            stats["stream"] += 1
    return stats


def import_income_shop(session: Session, records: list[dict]) -> dict:
    """开店 → 逐年 income_stream（时段内取 合并税后落袋 均值，挂 Henri Peeters）。"""
    stats = {"stream": 0}
    for rec in records:
        ent = upsert_entity(session, "person", rec["holder"])
        for y in range(rec["y0"], rec["y1"] + 1):
            session.add(IncomeStream(
                entity_id=ent.id, stream_type="shop", group_key="祖父开店",
                currency=rec.get("currency"), year=y, amount=rec["amount"],
                label="祖父开店 · 合并税后落袋",
                source_file=rec.get("source_file"),
            ))
            stats["stream"] += 1
    return stats


def import_fx(session: Session, records: list[dict]) -> dict:
    """汇率 → exchange_rate（幂等 on_conflict on (fx_from,fx_to,year) 近似）。"""
    from app.model import ExchangeRate
    stats = {"n": 0}
    if not records:
        return stats
    for rec in records:
        exists = session.execute(
            select(ExchangeRate.id).where(
                ExchangeRate.fx_from == rec["fx_from"], ExchangeRate.fx_to == rec["fx_to"],
                ExchangeRate.year == rec.get("year"))
        ).scalar_one_or_none()
        if exists is None:
            session.add(ExchangeRate(fx_from=rec["fx_from"], fx_to=rec["fx_to"],
                                     year=rec.get("year"), rate=rec["rate"]))
            stats["n"] += 1
    return stats


def import_return_curves(session: Session, records: list[dict]) -> dict:
    """收益测算表 → return_curve（幂等：ON CONFLICT DO NOTHING）。"""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from app.model import ReturnCurve
    stats = {"n": 0}
    if not records:
        return stats
    stmt = pg_insert(ReturnCurve).values([
        {"country": r["country"], "risk_lvl": r["risk_lvl"], "year": r["year"],
         "rate": r["rate"], "source_file": r.get("source_file")} for r in records
    ]).on_conflict_do_nothing(
        constraint="uq_return_country_risk_year"
    ).returning(ReturnCurve.id)
    stats["n"] = len(session.execute(stmt).scalars().all())
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
    """薪资 → 逐年 income_stream(salary)，各归各（养父/养母），取文件税后值。"""
    from app.model.types import StreamType
    stats = {"stream": 0}
    for rec in records:
        ent = upsert_entity(session, "person", rec["holder"])
        if rec.get("after_tax") is None:
            continue
        session.add(IncomeStream(
            entity_id=ent.id, stream_type=StreamType.SALARY.value, group_key=f"{rec['holder']}薪资",
            currency=rec.get("currency"), year=rec["year"], amount=rec["after_tax"],
            label=f"{rec['holder']}薪资税后", source_file=rec.get("source_file"),
        ))
        stats["stream"] += 1
    return stats


def import_household_expense(session: Session, records: list[dict]) -> dict:
    """家庭支出 → 逐年 ledger 支出（挂 Henri Peeters 账户，多年一致 BEF）。"""
    stats = {"n": 0, "skipped": 0}
    for rec in records:
        ent = upsert_entity(session, "person", rec["holder"])
        cur = rec.get("currency") or "BEF"
        acc = get_or_create_account(session, ent.id, cur)
        session.add(LedgerEntry(
            account_id=acc.id, date=resolve_date(rec["year"]), reason="家庭支出",
            outflow=rec["amount"], balance=None, kind="expense",
            source_file=rec.get("source_file"),
        ))
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
        # entity + account（唯一键：entity × currency × bank）
        ent = upsert_entity(session, "person", holder)
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