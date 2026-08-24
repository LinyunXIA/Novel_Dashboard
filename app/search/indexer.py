"""搜索索引入库（F-P1-08 · DESIGN §18.2）。

build_index：取各 source 条目 → 增量 upsert search_index（幂等 by 同 source+row+content）。
- 同名同内容且已有 embedding → 跳过；新/改内容 → embed 写入。
- 清理 stale 行（该 source 下 (row,content) 已不在当前快照的旧索引）→ 保持新鲜。
- source 指定则只建该类；整个跑完 = 全表慢索引（后台，不急）。

embedding 写入：PG 存 vector（float[] 原生）；SQLite 测试存 json 文本（VectorCompat 降级 Text）。
"""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.llm import embed
from app.model.search import SearchIndex


def _store_embedding(v: list[float], db: Session):
    # SQLite 测试（VectorCompat→Text）存 json；PG 存原生 float[] 给 vector 列
    if db.get_bind().dialect.name == "sqlite":
        return json.dumps(v)
    return v


def build_index(db: Session, source: str | None = None, *, client=None, log=None,
                batch_size: int = 32) -> dict:
    from app.search.extractors import EXTRACTORS
    sources = [source] if source else list(EXTRACTORS)
    stats = {"sources": 0, "inserted": 0, "skipped": 0, "stale_deleted": 0}

    for name in sources:
        if name not in EXTRACTORS:
            if log: log(f"  未知 source: {name}")
            continue
        items = list(EXTRACTORS[name](db))
        current = {(row_id, content) for row_id, content in items}

        # 既有索引行：key=(row_id, content) -> object
        existing: dict = {}
        for obj in db.execute(select(SearchIndex).where(SearchIndex.source_table == name)).scalars():
            existing[(obj.source_row_id, obj.content)] = obj
        # stale 清理
        for key, obj in list(existing.items()):
            if key not in current:
                db.delete(obj)
                stats["stale_deleted"] += 1
        # 待嵌入的新/改内容
        todo = [(r, c) for r, c in items if (r, c) not in existing]
        for i in range(0, len(todo), batch_size):
            chunk = todo[i:i + batch_size]
            texts = [c for _, c in chunk]
            vecs = embed(texts, client=client)          # 若 omlx 未起 → LlmUnavailable
            for (row_id, content), v in zip(chunk, vecs):
                db.add(SearchIndex(source_table=name, source_row_id=row_id,
                                   content=content, embedding=_store_embedding(v, db)))
                stats["inserted"] += 1
        stats["skipped"] += len(items) - len(todo)
        stats["sources"] += 1
        if log:
            log(f"  [{name}] 共{len(items)} 新增{len(todo)} 跳{len(items) - len(todo)} 删stale{stats['stale_deleted']}")
    return stats