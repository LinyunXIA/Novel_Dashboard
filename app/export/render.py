"""导出产物渲染（F-P2-07 · DESIGN §15）。

原则：**仅导出，不写任何源/输入文件**——只读 DB（源+覆盖层合并后的生效数据），
产物落 `config.exports_dir`（per-env，data/ 下不入 git）。格式：
- markdown：全库结构化档案（实体/编年史(覆盖优先)/账户/财务/收益/汇率），可作新素材
- csv：单表（scope = finance / returns / holdings / timeline / ledger）
- pdf：报告（家族总资产年度曲线内嵌 + 摘要 + 编年史近段），见 pdf.py
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import EnvConfig
from app.model import (Account, Entity, ExchangeRate, FinanceEntry, HoldingEvent,
                       IncomeStream, ReturnCurve, Snapshot, TimelineEvent)

ID_RE = re.compile(r"^[a-z]{3,8}-\d{8}T\d{6}-[0-9a-f]{6}$")
FORMATS = ("markdown", "csv", "pdf")
CSV_SCOPES = ("finance", "returns", "holdings", "timeline", "ledger")

_EXT = {"markdown": ".md", "csv": ".csv", "pdf": ".pdf"}
_CONTENT_TYPE = {"markdown": "text/markdown; charset=utf-8",
                 "csv": "text/csv; charset=utf-8",
                 "pdf": "application/pdf"}


def new_export_id(fmt: str) -> str:
    """自包含产物 id：`{fmt}-{yyyymmddTHHMMSS}-{hex6}`；GET 侧凭 ID_RE 校验防路径穿越。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{fmt[:8]}-{stamp}-{secrets.token_hex(3)}"


def export_path(cfg: EnvConfig, export_id: str) -> Path | None:
    """id → 磁盘产物路径；不匹配 ID_RE（穿越/伪造）或文件不存在 → None。"""
    fmt = export_id.split("-", 1)[0]
    if not ID_RE.match(export_id) or fmt not in FORMATS:
        return None
    p = cfg.exports_dir / f"{export_id}{_EXT[fmt]}"
    # 双保险：resolve 后必须仍位于 exports_dir 内（§15 仅导出、绝不触碰 source/input）
    if not p.resolve().is_relative_to(cfg.exports_dir.resolve()):
        return None
    return p if p.exists() else None


def content_type(export_id: str) -> str:
    return _CONTENT_TYPE[export_id.split("-", 1)[0]]


# --------------------------------------------------------------------------
# 生效数据读取（只读 DB；编年史按 §12 每 key 覆盖行优先）
# --------------------------------------------------------------------------
def effective_timeline(db: Session) -> list[TimelineEvent]:
    """每 (event_year, title) 一行：用户覆盖行（overlay:timeline: 前缀）优先于源行；
    系统 overlay 行（投资/划拨/活期 source_file=NULL）独立 key 原样保留。"""
    rows = db.execute(select(TimelineEvent).order_by(
        TimelineEvent.event_year, TimelineEvent.id)).scalars().all()
    best: dict[tuple[int, str], TimelineEvent] = {}
    for t in rows:
        k = (t.event_year, t.title)
        if t.source_file and t.source_file.startswith("overlay:timeline:"):
            best[k] = t
        else:
            best.setdefault(k, t)
    return sorted(best.values(), key=lambda x: (x.event_year, x.title))


def family_total_series(db: Session) -> list[tuple[int, float]]:
    """family:total 年度 USD 序列（§8 预计算快照层；PDF 图表与 md 摘要用）。"""
    rows = db.execute(select(Snapshot).where(
        Snapshot.scope == "family:total", Snapshot.as_of_date.is_(None),
        Snapshot.currency == "USD").order_by(Snapshot.as_of_year)).scalars().all()
    return [(r.as_of_year, float(r.value or 0)) for r in rows]


def _esc(v) -> str:
    s = "" if v is None else str(v)
    return s.replace("|", "\\|").replace("\n", " ")


# --------------------------------------------------------------------------
# markdown：全库结构化档案（§15「按当前生效数据渲染结构化 md」）
# --------------------------------------------------------------------------
def render_markdown(db: Session) -> str:
    ents = db.execute(select(Entity).order_by(Entity.entity_type, Entity.name)).scalars().all()
    accts = db.execute(select(Account).order_by(Account.id)).scalars().all()
    fins = db.execute(select(FinanceEntry).order_by(FinanceEntry.year)).scalars().all()
    streams = db.execute(select(IncomeStream).order_by(IncomeStream.year)).scalars().all()
    curves = db.execute(select(ReturnCurve).order_by(
        ReturnCurve.country, ReturnCurve.risk_lvl, ReturnCurve.year)).scalars().all()
    fxs = db.execute(select(ExchangeRate).order_by(
        ExchangeRate.fx_from, ExchangeRate.fx_to, ExchangeRate.year)).scalars().all()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    L: list[str] = [f"# 家族设定数据档案（导出于 {now}）", "",
                    "> Dashboard 导出生成：源 md + 覆盖层合并后的生效数据；仅导出不回写。", ""]

    ent_name: dict[int, str] = {}
    L += [f"## 一、实体（{len(ents)}）", "",
          "| 类型 | 名称 | 显示名 | status | 来源 |", "|---|---|---|---|---|"]
    for e in ents:
        ent_name[e.id] = e.name
        status = getattr(e, "status", None)
        L.append(f"| {e.entity_type} | {_esc(e.name)} | {_esc(e.display_name)} "
                 f"| {_esc(status)} | {e.source or 'file'} |")
    L.append("")

    tl = effective_timeline(db)
    L += [f"## 二、编年史（合并生效 {len(tl)} 条）", "",
          "| 年份 | 日期 | 标题 | 备注 | overlay |", "|---|---|---|---|---|"]
    for t in tl:
        d = t.event_date.isoformat() if t.event_date else ""
        L.append(f"| {t.event_year} | {d} | {_esc(t.title)} | {_esc(t.note)} "
                 f"| {'是' if t.overlay else ''} |")
    L.append("")

    L += [f"## 三、账户（{len(accts)}）", "",
          "| 实体 | 币种 | 银行 | 状态 | 关闭日 | 承接币 |", "|---|---|---|---|---|---|"]
    for a in accts:
        L.append(f"| {_esc(ent_name.get(a.entity_id, a.entity_id))} | {a.currency} "
                 f"| {_esc(a.bank)} | {a.status} | {a.closed_on or ''} "
                 f"| {a.migrate_to_currency or ''} |")
    L.append("")

    by_kind: dict[str, int] = {}
    for f in fins:
        by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
    L += [f"## 四、财务收支（finance_entry {len(fins)} 行 / 收益流 {len(streams)} 行）", "",
          "| kind | 行数 |", "|---|---|"]
    L += [f"| {k} | {n} |" for k, n in sorted(by_kind.items())] or ["| （空） | 0 |"]
    L.append("")

    latest: dict[str, int] = {}
    for c in curves:
        latest[c.country] = max(latest.get(c.country, 0), c.year or 0)
    L += [f"## 五、收益测算表（return_curve {len(curves)} 行；各国最新一年节选）", "",
          "| 国家 | R1 | R2 | R3 | R4 | R5 | 年份 |", "|---|---|---|---|---|---|---|"]
    for country in sorted(latest):
        yr = latest[country]
        vals = {c.risk_lvl: c.rate for c in curves if c.country == country and c.year == yr}
        L.append(f"| {country} | {vals.get('R1', '')} | {vals.get('R2', '')} "
                 f"| {vals.get('R3', '')} | {vals.get('R4', '')} | {vals.get('R5', '')} | {yr} |")
    L.append("")

    L += [f"## 六、汇率（exchange_rate {len(fxs)} 行）", "",
          "| 从 | 到 | 年份 | rate |", "|---|---|---|---|"]
    for x in fxs:
        L.append(f"| {x.fx_from} | {x.fx_to} | {x.year if x.year is not None else '常量'} "
                 f"| {x.rate} |")
    L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------
# csv：单表（§15「财务、收益、持仓等表格」）
# --------------------------------------------------------------------------
_CSV_HEADERS = {
    "finance": ["year", "entity_id", "entity_kind", "kind", "amount", "currency", "label", "source"],
    "returns": ["country", "risk_lvl", "year", "rate"],
    "holdings": ["entity_id", "company", "ticker", "date", "event_type", "batch_id",
                 "shares", "unit_price", "amount", "pct", "closed_on"],
    "timeline": ["event_year", "event_date", "title", "note", "overlay"],
    "ledger": ["account_id", "date", "reason", "kind", "inflow", "outflow", "balance"],
}


def render_csv(db: Session, scope: str) -> str:
    """scope 单表 CSV；值内含逗号/引号/换行按 RFC4180 转义。"""

    def q(v) -> str:
        s = "" if v is None else str(v)
        if any(ch in s for ch in ',"\n'):
            return '"' + s.replace('"', '""') + '"'   # RFC4180：包裹 + 内部引号翻倍
        return s

    lines: list[list] = [_CSV_HEADERS[scope]]
    if scope == "finance":
        for r in db.execute(select(FinanceEntry).order_by(FinanceEntry.year)).scalars().all():
            lines.append([r.year, r.entity_id, r.entity_kind, r.kind, r.amount,
                          r.currency, r.label, r.source])
    elif scope == "returns":
        for r in db.execute(select(ReturnCurve).order_by(
                ReturnCurve.country, ReturnCurve.risk_lvl, ReturnCurve.year)).scalars().all():
            lines.append([r.country, r.risk_lvl, r.year, r.rate])
    elif scope == "holdings":
        for r in db.execute(select(HoldingEvent).order_by(HoldingEvent.date)).scalars().all():
            lines.append([r.entity_id, r.company, r.ticker, r.date, r.event_type,
                          r.batch_id, r.shares, r.unit_price, r.amount, r.pct,
                          getattr(r, "closed_on", None)])
    elif scope == "timeline":
        for t in effective_timeline(db):
            lines.append([t.event_year,
                          t.event_date.isoformat() if t.event_date else "",
                          t.title, t.note, int(bool(t.overlay))])
    elif scope == "ledger":
        from app.model import LedgerEntry
        for r in db.execute(select(LedgerEntry).order_by(LedgerEntry.date)).scalars().all():
            lines.append([r.account_id, r.date, r.reason, r.kind,
                          r.inflow, r.outflow, r.balance])
    return "\n".join(",".join(q(v) for v in row) for row in lines) + "\n"
