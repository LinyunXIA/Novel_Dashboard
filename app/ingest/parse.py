"""统一 parse 分发（DESIGN §6）：detect → 对应 parser → 归一化记录。

失败不阻塞其它文件，进入 ingest_report（需人工处理）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.ingest import parsers
from app.ingest.detect import detect, scan_dir
from app.ingest.parsers import ParseError


@dataclass
class ParseResult:
    file: str
    category: str
    records: list = field(default_factory=list)
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
    def skipped(self) -> list[ParseResult]:
        return [r for r in self.results if r.category == "PHASE2_EVENT"]


_PARSERS = {
    "bank": parsers.parse_bank,
    "stock_tx": parsers.parse_stock_tx,
    "return_table": parsers.parse_return_table,
    "fx": parsers.parse_fx,
    "character": parsers.parse_character,
    "timeline": parsers.parse_timeline,
    "initial_asset": parsers.parse_initial_asset,
    "income_security": parsers.parse_income_security,
    "income_rent": parsers.parse_income_rent,
    # F-P0-05 其余 / F-P0-06：income_property/shop, salary, household_expense
    "income_property": None,
    "income_shop": None,
    "salary": None,
    "household_expense": None,
}


def parse_one(rel: str, path: Path) -> ParseResult:
    det = detect(rel)
    pr = ParseResult(file=rel, category=det.category)
    if det.phase2:
        pr.ok = False
        pr.error = "Phase 2 事件，Phase 1 跳过"
        return pr
    fn = _PARSERS.get(det.category)
    if fn is None:
        pr.ok = False
        pr.error = f"解析器未实现（{det.category}）"
        return pr
    try:
        pr.records = fn(path)
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