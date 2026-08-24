"""事件·电影解析（DESIGN §6.1 / §19.6 · Phase 2 占位）。

Phase 1 跳过（detect → PHASE2_EVENT），Phase 2 启用后由数据调整员导入 →
不关联账户 → UI 同币种手动关联。

当前为占位实现：解析返回空列表，保留接口以便 Phase 2 增量启用时
不改 detect/parse 分发，仅填充本文件逻辑。
"""
from __future__ import annotations

from pathlib import Path


def parse_event_movie(path: Path) -> list[dict]:
    """电影事件素材占位解析（Phase 2）。

    输入：基准/事件/电影/*.md（标题、成本、票房、分账等）
    输出：归一化记录（待 Phase 2 定义 schema）

    当前返回空列表，调用方（parse.py）已通过 det.phase2 提前拦截，
    不会实际进入本函数；保留以满足 DESIGN §3 目录结构完整性。
    """
    _ = path
    return []


# DESIGN §3 要求的统一入口名 parse
parse = parse_event_movie
