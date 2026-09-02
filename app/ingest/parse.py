"""统一 parse 分发（DESIGN §6）：detect → 对应 parser → 归一化记录。

失败不阻塞其它文件，进入 ingest_report（需人工处理）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.ingest import parsers
from app.ingest.detect import detect, scan_dir
from app.ingest.parsers import ParseError
from app.ingest.parsers.event_movie import parse_event_movie
from app.ingest.parsers.event_stock import parse_event_stock


@dataclass
class ParseResult:
    file: str
    category: str
    records: list = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ok: bool = True
    error: str | None = None


@dataclass
class IngestReport:
    results: list[ParseResult] = field(default_factory=list)

    @property
    def ok(self) -> list[ParseResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[ParseResult]:
        return [r for r in self.results if not r.ok]

    @property
    def warnings(self) -> list[tuple[str, str]]:
        """收集 (file, warning) 对，供上层展示。"""
        out: list[tuple[str, str]] = []
        for r in self.results:
            for w in r.warnings:
                out.append((r.file, w))
        return out

    @property
    def skipped(self) -> list[ParseResult]:
        """issue #26：SKIP_* 类别（P1 范围/创作约束）单独分组，不与 unknown 报错混淆。"""
        return [r for r in self.results if r.category.startswith("SKIP_")]


# 解析器签名有两种：(path) -> list 或 (path) -> (list, warnings)。
# 下面通过 _call 统一处理。
def _call(fn, path: Path) -> tuple[list, list[str]]:
    out = fn(path)
    if isinstance(out, tuple) and len(out) == 2:
        return out[0], list(out[1] or [])
    return out, []


_PARSERS = {
    "bank": parsers.parse_bank,
    "stock_tx": parsers.parse_stock_tx,
    "return_table": parsers.parse_return_table,
    "fx": parsers.parse_fx,
    "character": parsers.parse_character,
    "timeline": parsers.parse_timeline,
    "initial_asset": parsers.parse_initial_asset,
    # issue #211：基本收入.md（股债/房产/商业逐年终值）整合取代旧四类 income_* parser
    "basic_income": parsers.parse_basic_income,
    "salary": parsers.parse_salary,
    "household_expense": parsers.parse_household_expense,
    # Phase2 占位（DESIGN §6.1 / §19.6）：当前 Phase1 跳过，Parser 已就绪供 Phase2 启用
    "event_movie": parse_event_movie,
    "event_stock": parse_event_stock,
}


def parse_one(rel: str, path: Path) -> ParseResult:
    det = detect(rel)
    pr = ParseResult(file=rel, category=det.category)
    # Phase 2 事件交给对应 parser（event_movie / event_stock）；桩实现返 []，对 F-P2-02 留位。
    # issue #26：SKIP_* 类别（P1 范围/创作约束）显式跳过，不算 unknown 报错
    if det.category.startswith("SKIP_"):
        pr.ok = True
        return pr
    fn = _PARSERS.get(det.category)
    if fn is None:
        pr.ok = False
        pr.error = f"解析器未实现（{det.category}）"
        return pr
    try:
        pr.records, pr.warnings = _call(fn, path)
    except Exception as e:  # noqa: BLE001
        pr.ok = False
        pr.error = f"{type(e).__name__}: {e}"
    return pr


def run_ingest(input_dir: Path) -> IngestReport:
    """扫描 input_dir 并对每个可解析文件跑 parse（仅识别/解析，不落库）。"""
    report = IngestReport()
    for det in scan_dir(input_dir):
        path = input_dir / det.relpath
        if not path.exists() or not path.is_file():
            continue
        report.results.append(parse_one(det.relpath, path))
    return report