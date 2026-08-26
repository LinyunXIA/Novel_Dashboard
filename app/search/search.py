"""统一搜索检索+装配+serve 后处理（F-P1-08 · DESIGN §18.3/§18.6）。

search：embed 问题 → pgvector 余弦 top-k 召回条目标段 → LLM 装配最终答案 → serve 后处理。
数值铁律（issue #126 落地 §18.6）：
- 确定性回填：问题含财富/总资产意图+年份 → 直接从快照 family:total 取 USD 确定值注入装配；
- 后置校验：answer 中出现「命中条目 ∪ 问题 ∪ 回填值」之外的数字 → 整句剔除；
  全部被剔且存在回填意图时回退固定话术——LLM 永远不是数字的来源。
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.llm import chat, embed
from app.model.search import SearchIndex

SYSTEM_PROMPT = (
    "你是网文创作数据 Dashboard 的检索问答助手。"
    "仅根据用户提供的【命中条目】与【确定性数据】作答，严禁编造事实。"
    "绝不自造任何数字、金额、汇率、百分比——所有数值只能原样引用上述材料里出现的数字；"
    "若材料未提供所需数值，回答「资料未提供」。"
    "只输出最终答案正文：不要复述问题、不要分步编号、不要输出推理/思考/分析过程、不要列来源。"
    "用中文。"
)

TOP_K = 8

_WEALTH_INTENT_WORDS = ("总资产", "财富", "净值", "家族资产", "资产合计")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _canon_num(tok: str) -> str:
    s = tok.lstrip("0") or "0"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _num_tokens(text: str) -> set[str]:
    """去千分位后抽取数字 token 并规范化（'1,234.50'→'1234.5'）。"""
    return {_canon_num(x) for x in _NUM_RE.findall((text or "").replace(",", ""))}


def _backfill_wealth(db: Session, question: str) -> list[str]:
    """确定性回填（§18.6）：财富意图 + 年份 → 快照 family:total(年聚合, USD)。"""
    if not any(w in question for w in _WEALTH_INTENT_WORDS):
        return []
    lines: list[str] = []
    from app.model import Snapshot
    for y in sorted({int(x) for x in re.findall(r"(?:19|20)\d{2}", question)}):
        if not 1947 <= y <= 2026:
            continue
        row = db.execute(
            select(Snapshot.value, Snapshot.currency).where(
                Snapshot.scope == "family:total",
                Snapshot.as_of_year == y,
                Snapshot.as_of_date.is_(None),
            ).limit(1)
        ).first()
        if row and row[0] is not None:
            v = float(row[0])
            yi = int(v / 1e8 * 100) / 100     # 预置亿/万缩放变体，允许 LLM 用中文数量级复述
            w = int(v / 1e4 * 10) / 10
            lines.append(f"{y} 年家族总资产（快照确定值，展示折USD）= "
                         f"{v:,.2f} USD（约 {yi} 亿 / {w} 万 USD）")
    return lines


def numeric_guard(answer: str, allowed_texts: list[str]) -> str:
    """后置校验（issue #126）：剔除携带未知数字的句子；无数字的句子不受影响。

    若所有句子都因未知数字被剔 → 回退固定话术（宁可少答，不可错答）。
    """
    allowed: set[str] = set()
    for t in allowed_texts or []:
        allowed |= _num_tokens(t)
    kept: list[str] = []
    dropped_any = False
    for sent in re.split(r"(?<=[。！？!?\n])|(?:\n)", answer or ""):
        sent = sent.strip()
        if not sent:
            continue
        toks = _num_tokens(sent)
        if toks and not toks <= allowed:
            dropped_any = True
            continue
        kept.append(sent)
    out = " ".join(kept).strip()
    if not out and dropped_any:
        return "资料未提供相关确定性数值。"
    return out


def retrieve(db: Session, query_vec: list[float], k: int = TOP_K) -> list[dict]:
    """pgvector 余弦 top-k（embedding <=> query_vec，越小越近）。仅 PG；SQLite 测试用 monkeypatch。"""
    rows = db.execute(
        select(SearchIndex.content, SearchIndex.source_table, SearchIndex.source_row_id)
        .where(SearchIndex.embedding.isnot(None))
        .order_by(SearchIndex.embedding.cosine_distance(query_vec))
        .limit(k)
    ).all()
    return [{"content": c, "source_table": t, "source_row_id": r} for c, t, r in rows]


def clean_answer(text: str) -> str:
    """serve 后处理（§18.6）：剥推理/复述头、去掉分步编号，只留最终答案主体。

    去步骤编号只处理 `1. / 1、/ 2)` 形式（数字后必随分隔符）；「1947年」这类数字紧接字符的
    内容（年份/金额）一律保留，不剥。"""
    t = (text or "").strip()
    lines = [ln.strip() for ln in t.splitlines()]
    kept = []
    marker_done = False  # 跳过开头纯包装（答案：/答：/分析/思考）
    for ln in lines:
        if not ln:
            if kept:
                break
            continue
        if not marker_done and re.fullmatch(r"(分析|思考|推理|步骤|回答)[:：\-]?\s*.*", ln):
            continue
        if re.match(r"^\d+\s*[\.、)]\s*.+", ln):  # 步骤编号：1. / 1、 / 2)
            ln = re.sub(r"^\d+\s*[\.、)]\s*", "", ln)
        kept.append(ln)
        marker_done = True
    out = " ".join(kept).strip()
    out = re.sub(r"^(答案|答|结论)[:：]\s*", "", out)
    return out


def search(db: Session, question: str, as_of: str | None = None, *,
           client=None, k: int = TOP_K) -> dict:
    """问题→答案。{answer, hits[]}；hits 内部用（前端只渲染 answer）。"""
    q_vec = embed([question], client=client)[0]
    hits = retrieve(db, q_vec, k)
    if not hits:
        return {"answer": "索引中暂无与此问题相关的条目（可先运行 `search-index` 建索引，或换一个问法）。",
                "hits": []}
    backfill = _backfill_wealth(db, question)
    candidate = "\n".join(f"[{h['source_table']}#{h['source_row_id']}] {h['content']}" for h in hits)
    user = f"问题：{question}\n\n【命中条目】\n{candidate}"
    if backfill:
        user += ("\n\n【确定性数据】（快照/SQL 计算结果，涉及这些数值时必须原样引用，"
                 "不得改写或另行计算）\n" + "\n".join(backfill))
    raw = chat(SYSTEM_PROMPT, user, client=client)
    answer = clean_answer(raw)
    answer = numeric_guard(answer, allowed_texts=[question] +
                           [h["content"] for h in hits] + backfill)
    return {"answer": answer, "hits": hits}