"""事件·股票解析（DESIGN §6.1 / §19.6 · Phase 2 占位）。

Phase 1 跳过（detect → PHASE2_EVENT），Phase 2 启用后：
  基准/事件/股票/** → holding_event(batch) + ledger_entry(买入/卖出/分红)
  由数据调整员导入不关联账户，UI 同币种手动关联。

当前为占位实现：解析返回空列表，保留接口与 DDL 对齐（holding_event.batch_id
FIFO、分拆/并购成本链见 DESIGN §19.6）。
"""
from __future__ import annotations

from pathlib import Path


def parse_event_stock(path: Path) -> list[dict]:
    """股票事件素材占位解析（Phase 2）。

    输入：基准/事件/股票/**（收购、分拆、并购链等）
    输出：归一化 holding_event + ledger_entry 记录（待 Phase 2 定义）

    当前返回空列表，调用方已通过 det.phase2 提前拦截；保留以满足
    DESIGN §3 目录结构完整性与后续增量开发不改分发逻辑。
    """
    _ = path
    return []


# DESIGN §3 要求的统一入口名 parse
parse = parse_event_stock
