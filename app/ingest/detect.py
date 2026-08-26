"""文件→类别识别（DESIGN §6.1）。

按 `input_dir 下的相对路径` 匹配；`基准/事件/` 为 Phase1 阶段项（跳过，Phase2 启用）。

issue #26 修复：
- `基准/CPI工资.md` 显式映射（不再落 unknown 报噪音）
- `基准/公司/用工成本/` P1 范围 → `SKIP_P1`（DESIGN §13；Phase 1 不实现，跳过不报错）
- `设计文件/` 创作约束笔记 → `SKIP_DOC`（不入库）
SKIP_* 类别在 parse_one 显式跳过，不计入 unknown 报错。

issue #70：CPI工资.md 从 `cpi_wage` 改归 `SKIP_PARAM`——它是折算/展示基准参数，
当前无任何消费方；原映射导致「有类别无 parser」每轮报 ❌ 解析器未实现。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# (前缀, 类别) —— 顺序敏感：更长/更精确前缀在前
_PREFIX_RULES: list[tuple[str, str]] = [
    ("基准/事件/电影/", "event_movie"),       # Phase2 占位（DESIGN §6.1 / §19.6）
    ("基准/事件/股票/", "event_stock"),       # Phase2 占位（DESIGN §6.1 / §19.6）
    # issue #144：散文件兜底改归 SKIP_*——§6.1「Phase 1 直接跳过」语义，
    # 不再落无 parser 类别每轮报 ❌ 解析器未实现（event CLI 单独导入，不走扫描链）
    ("基准/事件/", "SKIP_PHASE2_EVENT"),
    ("基准/CPI工资.md", "SKIP_PARAM"),        # issue #70：CPI 与工资增幅基准参数，无消费方显式跳过
    ("基准/公司/用工成本/", "SKIP_P1"),        # issue #26：P1 §13 范围，Phase1 跳过
    ("设计文件/", "SKIP_DOC"),                # issue #26：创作约束笔记，不入库
    ("经济/银行/", "bank"),
    ("经济/股票/", "stock_tx"),
    ("基准/收益表/惠民租房.md", "income_rent"),
    ("基准/收益表/经营性房产收益.md", "income_property"),
    ("基准/收益表/祖产股票债券收益.md", "income_security"),
    ("基准/收益表/祖父开店.md", "income_shop"),
    ("基准/收益表/1974-2001家庭支出.md", "household_expense"),
    ("基准/收益表/", "return_table"),
    ("基准/初始资产/", "initial_asset"),
    ("基准/薪资/", "salary"),
    ("基准/1974-2001家庭支出.md", "household_expense"),
    ("基准/汇率/", "fx"),
    ("人物/", "character"),
    ("时间线.md", "timeline"),
]

# Phase2 类别集合（需数据调整员导入后 UI 关联，Phase1 跳过）
PHASE2_CATEGORIES = {"event_movie", "event_stock", "SKIP_PHASE2_EVENT"}


def is_skip_category(category: str) -> bool:
    """issue #26：SKIP_* 类别在 parse_one 显式跳过；供调用方判定。"""
    return category.startswith("SKIP_")


@dataclass(frozen=True)
class Detected:
    relpath: str
    category: str
    phase2: bool = False


def detect(rel: str) -> Detected:
    """按相对路径返回类别。rel 用正斜杠同 input_dir 下相对路径。"""
    rel = rel.strip().lstrip("/")
    for prefix, cat in _PREFIX_RULES:
        if rel.startswith(prefix):
            return Detected(rel, cat, cat in PHASE2_CATEGORIES)
    return Detected(rel, "unknown")


def scan_dir(input_dir: Path) -> list[Detected]:
    """扫描输入目录，收集所有 .md 的识别结果（阶段项标记但留给 ingest 跳过）。"""
    out: list[Detected] = []
    if not input_dir.exists():
        return out
    for md in sorted(input_dir.rglob("*.md")):
        rel = md.relative_to(input_dir).as_posix()
        out.append(detect(rel))
    return out