"""事件·股票链编排器（F-P2-04 · DESIGN §19.6）。

把一段重组链（购入建仓 → 并购/分拆 → 减持 → 现金退出）编码为**事件序列**，按日期顺序
驱动 `stock_cost` 引擎（apply_buy / apply_merger / apply_sell / apply_dividend /
apply_passive_uplift）应用，并提供 `verify_chain` 做「依 H2 逐行验证」（只读对账）。

幂等：每步 event_id（缺省自动生成 `chain:{name}:{seq:03d}:{type}:{company}`）写入
HoldingEvent.source_file / ledger note，整链重放全 skipped。撤销约定复用引擎 tag
（`note LIKE '%股票事件·{event_id}%'` 删 ledger；`source_file==event_id` 删 holding）。

事件格式（每 step 一个 dict）：
    {"type":"buy","company","date","unit_price","shares","account_id","ticker"?,"event_id"?,"calibrated"?}
    {"type":"split","company"(old_company),"date","legs":[{"company","per_old_share","ticker"?}]}
    {"type":"cash_share","company","date","legs":[...],"cash_per_share","cash_account_id"}
    {"type":"cash","company","date","cash_per_share","cash_account_id"}
    {"type":"sell","company","date","shares","sell_price","account_id"}
    {"type":"dividend","company","date","per_share","account_id"}
    {"type":"passive_uplift","company","date","to_pct","ticker"?}
    "calibrated": True = 回测校准非史实（仅标注意义，不参与计算）
"""
from __future__ import annotations

from datetime import date as _date

from sqlalchemy.orm import Session

from app.model import HoldingEvent, LedgerEntry
from sqlalchemy import select

from app.core.stock_cost import (apply_buy, apply_dividend, apply_merger,
                                 apply_passive_uplift, apply_sell, _open_batches)

#: 每步可识别的 type
_STEP_TYPES = {"buy", "split", "cash_share", "cash", "sell", "dividend", "passive_uplift"}


def _event_id_of(chain_name: str, seq: int, step: dict) -> str:
    return step.get("event_id") or f"chain:{chain_name}:{seq:03d}:{step['type']}:{step['company']}"


def apply_chain(session: Session, chain: dict, accounts: dict[str, int] | None = None,
                commit: bool = True) -> dict:
    """按日期顺序应用整条链。accounts：{role: account_id} 兜底（role ∈ cash/buy/sell/dividend）。"""
    name = chain.get("name", "chain")
    entity_id = chain["entity_id"]
    # 稳定排序（只按 date）：同 date 保持列表写入序（如 2017-04-01 需 HPE→DXC 先、CSC→DXC 再、DXC sell 最后）
    steps = sorted(chain["steps"], key=lambda s: _date.fromisoformat(s["date"]))
    out_steps: list[dict] = []
    calibrated: list[int] = []
    applied = skipped = 0
    for seq, step in enumerate(steps, start=1):
        typ = step["type"]
        if typ not in _STEP_TYPES:
            raise ValueError(f"未知 step type: {typ}")
        event_id = _event_id_of(name, seq, step)
        if step.get("calibrated"):
            calibrated.append(seq)
        res = _dispatch(session, entity_id, step, event_id, accounts or {})
        ok = not res.get("skipped", False)
        applied += ok
        skipped += (not ok)
        out_steps.append({"seq": seq, "type": typ, "company": step["company"],
                          "date": step["date"], "event_id": event_id,
                          "skipped": not ok, "calibrated": bool(step.get("calibrated"))})
    if commit:
        session.commit()
    return {"chain": name, "applied": applied, "skipped": skipped,
            "steps": out_steps, "calibrated_steps": calibrated}


def _role(v, accounts: dict[str, int]):
    """把 `@role` 哨兵解析成 accounts 里的角色 id；普通 int 原样返回。"""
    if isinstance(v, str) and v.startswith("@") :
        return accounts.get(v[1:])
    return v


def _dispatch(session: Session, entity_id: int, step: dict, event_id: str,
              accounts: dict[str, int]) -> dict:
    typ = step["type"]
    company = step["company"]
    d = _date.fromisoformat(step["date"])
    aid = _role(step.get("account_id"), accounts) or \
        accounts.get("buy") or accounts.get("cash")
    cash_aid = _role(step.get("cash_account_id"), accounts) or accounts.get("cash")

    if typ == "buy":
        return apply_buy(session, entity_id=entity_id, company=company,
                         ticker=step.get("ticker"), date=d,
                         unit_price=step["unit_price"], shares=step["shares"],
                         event_id=event_id, account_id=aid)
    if typ == "split" or typ == "cash_share" or typ == "cash":
        spec = {"entity_id": entity_id, "date": step["date"], "old_company": company,
                "form": typ, "legs": step.get("legs") or [],
                "cash_per_share": step.get("cash_per_share"),
                "cash_account_id": cash_aid or step.get("cash_account_id")}
        return apply_merger(session, spec, source=event_id)
    if typ == "sell":
        return apply_sell(session, entity_id=entity_id, company=company, date=d,
                          shares=step["shares"], sell_price=step["sell_price"],
                          event_id=event_id, account_id=aid)
    if typ == "dividend":
        return apply_dividend(session, entity_id=entity_id, company=company, date=d,
                              per_share=step["per_share"], event_id=event_id,
                              account_id=aid or accounts.get("dividend"))
    if typ == "passive_uplift":
        return apply_passive_uplift(session, entity_id=entity_id, company=company, date=d,
                                    to_pct=step.get("to_pct"), event_id=event_id,
                                    ticker=step.get("ticker"))
    raise ValueError(f"未知 step type: {typ}")


def _open_shares(session: Session, entity_id: int, company: str) -> float:
    return sum(b["shares"] for b in _open_batches(session, entity_id, company))


def verify_chain(session: Session, chain: dict, expected: list[dict],
                 as_of: _date, tol: float = 1.0) -> dict:
    """逐行验证（F-P2-04「依 H2 逐行验证」）。只读，不改库。

    expected 元素：
      {"open": True, "company": str, "shares": 期望股数}           → 用 _open_batches 求和比对
      {"open": False, "company": str, "shares": 0} (或 期望已结清)  → DB 无该 open → closed match
      {"ledger_kind": str, "note_like": str, "amount": 期望现金}   → 按 ledger note LIKE 求和比对

    返回 {ok, rows, cash, problems, warnings}。DB 有但未在 expected 的公司标 unasserted
    （informational，不入 problems）。
    """
    eid = chain["entity_id"]
    problems: list[str] = []
    warnings: list[str] = []
    rows: list[dict] = []
    cash_rows: list[dict] = []

    # 现存 open 公司集合（供 unasserted 检测）
    open_companies = set()
    holds = session.execute(select(HoldingEvent).where(
        HoldingEvent.entity_id == eid,
        HoldingEvent.shares > 0,
        HoldingEvent.closed_on.is_(None))).scalars().all()
    open_companies = {h.company for h in holds}

    asserted = set()
    # expected 元素分两类：{company, shares, open?} 持股断言 / {ledger_kind, note_like 或 reason_like, amount} 现金断言
    for x in expected:
        if "ledger_kind" in x:
            # 现金断言：按 kind + note_like 或 reason_like 求和
            q = select(LedgerEntry.inflow, LedgerEntry.outflow).where(
                LedgerEntry.kind == x["ledger_kind"])
            if x.get("note_like"):
                q = q.where(LedgerEntry.note.like(f"%{x['note_like']}%"))
            elif x.get("reason_like"):
                q = q.where(LedgerEntry.reason.like(f"%{x['reason_like']}%"))
            else:
                raise ValueError("现金断言需 note_like 或 reason_like")
            amt = session.execute(q).all()
            actual = sum(float(a or 0) - float(b or 0) for a, b in amt)
            # 现金容差：亚股 float 漂移可容忍，但不放宽到真实级差（`|amt|*1e-3` 过松 → 收紧为 *1e-6）
            match = abs(actual - x["amount"]) <= max(1.0, abs(x["amount"]) * 1e-6)
            key = x.get("reason_like") or x.get("note_like")
            cash_rows.append({"key": key, "actual": actual,
                              "expected": x["amount"], "match": bool(match)})
            if not match:
                problems.append(f"cash[{key}] 实际 {actual:,.2f} ≠ 期望 {x['amount']:,.2f}")
            continue
        asserted.add(x["company"])
        actual = _open_shares(session, eid, x["company"])
        if x.get("open", True):
            match = abs(actual - x["shares"]) <= tol
            rows.append({"company": x["company"], "actual": actual,
                         "expected": x["shares"], "match": bool(match),
                         "note": "ok" if match else "mismatch"})
            if not match:
                problems.append(f"{x['company']} 实际 {actual:,.2f} 股 ≠ 期望 {x['shares']:,.2f}")
        else:
            # 期望已结清：DB 应无该 open
            match = actual <= tol
            rows.append({"company": x["company"], "actual": actual, "expected": 0,
                         "match": bool(match), "note": "closed" if match else "mismatch"})
            if not match:
                problems.append(f"{x['company']} 应已结清，实剩 {actual:,.2f} 股")

    for comp in sorted(open_companies - asserted):
        rows.append({"company": comp, "actual": _open_shares(session, eid, comp),
                     "expected": None, "match": None, "note": "unasserted"})
        warnings.append(f"{comp} open 但未在 expected 中（unasserted）")

    return {"chain": chain.get("name"), "as_of": as_of.isoformat(),
            "ok": not problems, "rows": rows, "cash": cash_rows,
            "problems": problems, "warnings": warnings}


def chain_cash_total(session: Session, chain: dict, note_like: str = "股权事件·cash") -> float:
    """链累计现金对价（F-P2-04 可选断言：前端/报告汇总）。"""
    rows = session.execute(
        select(LedgerEntry.inflow).where(LedgerEntry.note.like(f"%{note_like}%"))).all()
    return sum(float(a or 0) for a, in rows)