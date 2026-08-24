"""统一搜索检索+装配+serve 后处理（F-P1-08 · DESIGN §18.3/§18.6）。

search：embed 问题 → pgvector 余弦 top-k 召回条目标段 → LLM 装配最终答案 → serve 后处理。
数值铁律：LLM 只原样引用命中条目里的数值，不自造（命中条目由 DB 派生含确定值）。
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.llm import chat, embed
from app.model.search import SearchIndex

SYSTEM_PROMPT = (
    "你是网文创作数据 Dashboard 的检索问答助手。"
    "仅根据用户提供的【命中条目】作答，严禁编造事实。"
    "绝不自造任何数字、金额、汇率、百分比——所有数值只能原样引用【命中条目】里出现的数字；"
    "若命中条目未提供所需数值，回答「资料未提供」。"
    "只输出最终答案正文：不要复述问题、不要分步编号、不要输出推理/思考/分析过程、不要列来源。"
    "用中文。"
)

TOP_K = 8


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
    candidate = "\n".join(f"[{h['source_table']}#{h['source_row_id']}] {h['content']}" for h in hits)
    user = f"问题：{question}\n\n【命中条目】\n{candidate}"
    raw = chat(SYSTEM_PROMPT, user, client=client)
    return {"answer": clean_answer(raw), "hits": hits}