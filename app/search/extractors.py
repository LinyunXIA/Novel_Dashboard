"""统一搜索条目提取器（F-P1-08 · DESIGN §18.2）。

EXTRACTORS：source_table → 迭代 (row_id, content 中文描述句)。粒度=条目/行级。
content 含确定性数值（DB 派生），供 LLM 装配时原样引用、不编数（§18.6 数值铁律）。

先覆盖语义表；技术表（notification/recompute_job/date_rule/source_file_version/user_data_overlay）
不索引，可后续增补。
"""
from __future__ import annotations

from typing import Callable, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.model import (
    Account, Entity, ExchangeRate, FinanceEntry, HoldingEvent, IncomeStream,
    InitialAsset, Investment, LedgerEntry, Relationship, ReturnCurve, TimelineEvent,
)
from app.model.labor import LaborCpiGrowth, LaborTaxBenchmark, LaborWageBenchmark


def _say(pairs: list) -> str:
    """[('人物', '亨利'), ('状态','在营')] → '人物亨利，状态在营'。"""
    return "，".join(f"{k}{v}" for k, v in pairs if v)

# ---- 各表提取器（yield (row_id, content)） ----
_ENTITY_TYPE = {"person": "人物", "company": "公司", "asset": "资产", "family": "家族"}


def _entity(db: Session) -> Iterator[tuple[int, str]]:
    for e in db.execute(select(Entity)).scalars().all():
        yield e.id, _say([(_ENTITY_TYPE.get(e.entity_type, e.entity_type), e.display_name or e.name),
                          ("状态", e.status)])
        if e.fields:
            yield e.id, _say([("实体", e.name), *[(k, v) for k, v in e.fields.items() if v]])


def _timeline(db: Session) -> Iterator[tuple[int, str]]:
    for t in db.execute(select(TimelineEvent)).scalars().all():
        yield t.id, _say([("年份", t.event_year), ("事件", t.title), ("说明", t.note)])


def _ledger(db: Session) -> Iterator[tuple[int, str]]:
    for e in db.execute(select(LedgerEntry)).scalars().all():
        yield e.id, _say([("流水日期", e.date), ("账户", e.account_id), ("事由", e.reason),
                          ("收入", e.inflow), ("支出", e.outflow), ("余额", e.balance),
                          ("类别", e.kind)])


def _finance(db: Session) -> Iterator[tuple[int, str]]:
    for e in db.execute(select(FinanceEntry)).scalars().all():
        yield e.id, _say([("年份", e.year), ("主体类别", e.entity_kind), ("主体", e.entity_id),
                          ("类别", e.kind), ("金额", e.amount), ("币种", e.currency),
                          ("标签", e.label), ("来源", e.source)])


def _income(db: Session) -> Iterator[tuple[int, str]]:
    for e in db.execute(select(IncomeStream)).scalars().all():
        yield e.id, _say([("年份", e.year), ("收益类型", e.stream_type), ("分组", e.group_key),
                          ("金额", e.amount), ("币种", e.currency), ("标签", e.label)])


def _holding(db: Session) -> Iterator[tuple[int, str]]:
    for e in db.execute(select(HoldingEvent)).scalars().all():
        yield e.id, _say([("标的", e.company), ("代码", e.ticker), ("日期", e.date),
                          ("事件", e.event_type), ("股数", e.shares), ("成本价", e.unit_price),
                          ("金额万美金", e.amount), ("批次", e.batch_id)])


def _return(db: Session) -> Iterator[tuple[int, str]]:
    for e in db.execute(select(ReturnCurve)).scalars().all():
        yield e.id, _say([("国家", e.country), ("风险级", e.risk_lvl), ("年份", e.year),
                          ("年化", f"{e.rate}%")])


def _fx(db: Session) -> Iterator[tuple[int, str]]:
    for e in db.execute(select(ExchangeRate)).scalars().all():
        yield e.id, _say([("汇率", f"{e.fx_from}->{e.fx_to}"), ("年份", e.year), ("值", e.rate)])


def _account(db: Session) -> Iterator[tuple[int, str]]:
    for e in db.execute(select(Account)).scalars().all():
        yield e.id, _say([("账户", e.entity_id), ("币种池", e.currency), ("状态", e.status),
                          ("开户行", e.bank), ("关池日", e.closed_on)])


def _relationship(db: Session) -> Iterator[tuple[int, str]]:
    for e in db.execute(select(Relationship)).scalars().all():
        yield e.id, _say([("关系", f"{e.from_entity_id} {e.rel_type} {e.to_entity_id}"),
                          ("起", e.since_year), ("止", e.until_year)])


def _initial_asset(db: Session) -> Iterator[tuple[int, str]]:
    for e in db.execute(select(InitialAsset)).scalars().all():
        yield e.id, _say([("主体", e.entity_id), ("资产类型", e.asset_type), ("分组", e.group_key),
                          ("币种", e.currency), ("名称", e.name), ("面值", e.face_value),
                          ("占比", f"{e.pct}%")])


def _investment(db: Session) -> Iterator[tuple[int, str]]:
    for e in db.execute(select(Investment)).scalars().all():
        yield e.id, _say([("年份", e.year), ("地区", getattr(e, "region", None)),
                          ("风险级", e.risk_lvl), ("金额", getattr(e, "amount", None))])


def _labor_wage(db: Session) -> Iterator[tuple[int, str]]:
    for e in db.execute(select(LaborWageBenchmark)).scalars().all():
        yield e.id, _say([("地区", e.region), ("年份", e.year), ("投资/金融行业年薪", e.investment_fin_salary),
                          ("全行业人均", e.avg_salary), ("币种", e.currency)])


def _labor_tax(db: Session) -> Iterator[tuple[int, str]]:
    for e in db.execute(select(LaborTaxBenchmark)).scalars().all():
        yield e.id, _say([("office", e.office), ("年份", e.year), ("成本模型", e.formula),
                          ("费率/上限", {k: v for k, v in (e.params or {}).items() if v is not None})])


def _labor_cpi(db: Session) -> Iterator[tuple[int, str]]:
    for e in db.execute(select(LaborCpiGrowth)).scalars().all():
        yield e.id, _say([("地区", e.region), ("年份", e.year),
                          ("工资增幅%", e.wage_growth_pct), ("CPI通胀%", e.cpi_pct)])


EXTRACTORS: dict[str, Callable[[Session], Iterator[tuple[int, str]]]] = {
    "entity": _entity,
    "timeline_event": _timeline,
    "ledger_entry": _ledger,
    "finance_entry": _finance,
    "income_stream": _income,
    "holding_event": _holding,
    "return_curve": _return,
    "exchange_rate": _fx,
    "account": _account,
    "relationship": _relationship,
    "initial_asset": _initial_asset,
    "investment": _investment,
    "labor_wage_benchmark": _labor_wage,
    "labor_tax_benchmark": _labor_tax,
    "labor_cpi_growth": _labor_cpi,
}