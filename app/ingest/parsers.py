"""基础解析器（DESIGN §6.3）：bank / stock_tx / return_table / fx / character / timeline。

每个 parse(path) -> list[dict] 归一化记录；失败抛 ParseError 由上层进 ingest_report。
F-P0-02：基础形态解析；收益/初始资产/薪资/支出的细粒度挂账在 F-P0-04/05/06。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from app.ingest.normalize import parse_number, resolve_date, currency_from


class ParseError(Exception):
    """单文件解析失败；进入 ingest_report 需人工处理。"""


@dataclass
class Row:
    cells: list[str]


def _lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _split_table_row(line: str) -> list[str]:
    """拆 Markdown 表格行；容错 `|`。"""
    s = line.strip()
    if not s.startswith("|"):
        return []
    s = s.strip("|")
    return [c.strip() for c in s.split("|")]


def _rows_between(lines: list[str], start: int, header_needs: list[str]) -> tuple[list[Row], int]:
    """从 line[start] 起找第一个含所有 header_needs 的表格，返回 (数据行, 结束行号)。
    返回行号供后续指针推进。
    """
    i = start
    while i < len(lines):
        cells = _split_table_row(lines[i])
        if len(cells) >= 2 and all(any(h in c for c in cells) for h in header_needs):
            # 表头下可能是分隔行 `|---|`
            j = i + 1
            rows: list[Row] = []
            while j < len(lines):
                rc = _split_table_row(lines[j])
                if not rc:
                    break
                if j == i + 1 and rc and all(not c.strip() for c in rc[1:]):
                    j += 1
                    continue
                rows.append(Row(rc))
                j += 1
            return rows, j
        i += 1
    return [], i


# ---------------- fx（汇率） ----------------
# 币种名 → 代码（TSV 表格列的 Currency 名）
_CUR_NAME = {
    "dutch": "NLG", "guilder": "NLG", "guilders": "NLG",
    "swedish": "SEK", "krona": "SEK",
    "danish": "DKK", "krone": "DKK",
    "belgian": "BEF", "franc": "BEF",
    "luxembourg": "LUF", "usd": "USD", "dollar": "USD",
    "euro": "EUR", "hkd": "HKD", "yuan": "CNY", "rmb": "CNY",
}


def parse_fx(path: Path) -> list[dict]:
    """支持两种格式（DESIGN §3）：

    - `1EUR=40.3399BEF` / `1 美元 = 8.2789 元人民币`（格式二）
    - TSV 表 `Currency  Code  Rate(Jan-1995)`，Rate = 1 USD 兑该币 → USD→{Currency}
    """
    recs: list[dict] = []
    cur_year = _detect_year_in_lines(_lines(path)) or _year_from_name(path.name)
    for line in _lines(path):
        s = line.strip()
        # 格式一：1XXX = rate YYY
        m = re.search(r"1\s*([A-Za-z]{2,4})\s*=\s*([\d.,]+)\s*([A-Za-z一-鿿]{2,6})", s, re.I)
        if m:
            recs.append({
                "fx_from": m.group(1).upper(), "rate": parse_number(m.group(2)),
                "fx_to": _cur(m.group(3)), "year": cur_year,
            })
            continue
        # 格式二：TSV 表格行  `Belgian Franc  BEF  32.14`
        t = re.fullmatch(r"([A-Za-z一-鿿]+(?:\s+[A-Za-z一-鿿]+)?)\s+([A-Z]{3})\s+([\d.,]+)", s)
        if t:
            code = _cur(t.group(1)) or t.group(2).upper()
            recs.append({"fx_from": "USD", "rate": parse_number(t.group(3)),
                         "fx_to": code, "year": cur_year})
    return recs


def _cur(name: str) -> str:
    """币种名/代码 → 标准代码；无法识别返回大写原样。"""
    n = name.strip().lower()
    if n.upper() in ("USD", "EUR", "BEF", "LUF", "NLG", "DKK", "SEK", "HKD", "CNY"):
        return n.upper()
    for k, v in _CUR_NAME.items():
        if k in n:
            return v
    return name.strip().upper()


def _year_from_name(name: str) -> int | None:
    fm = re.search(r"(19|20)\d{2}", name)
    return int(fm.group(0)) if fm else None


def _detect_year_in_lines(lines: list[str]) -> int | None:
    for x in lines:
        fm = re.search(r"((?:19|20)\d{2})", x)
        if fm:
            return int(fm.group(1))
    return None


# ---------------- return_table（收益测算表） ----------------
def parse_return_table(path: Path) -> list[dict]:
    """支持两种格式（欧洲/美国/英国=逐行 `- R1：x%`；香港/中国=竖线分隔单行 `R1：x｜R2：y｜…`）。

    年份来自 `#### 1999（…）` / `## 1999年` 标题。
    """
    lines = _lines(path)
    recs: list[dict] = []
    year = None
    region = _region_from_file(path.name)
    for line in lines:
        # 年份标题（多级 # 均可）
        ym = re.search(r"^#+\s*(?:\d{4}[-–]\d{4}\s+)?(19|20)\d{2}", line)
        if ym:
            fm = re.search(r"(19|20)\d{2}", ym.group(0))
            if fm:
                year = int(fm.group(0))
        # 格式A：竖线分隔 `R1：4.35｜R2：6.12｜R3：8.64｜R4：68.82｜R5：76.35`
        vm = re.finditer(r"R([1-5])\s*[:：]\s*([-+]?\d+(?:\.\d+)?)", line)
        pairs = [(int(m.group(1)), parse_number(m.group(2))) for m in vm]
        if pairs and len(pairs) >= 3 and year:
            for rl, rate in pairs:
                recs.append({"country": region, "risk_lvl": f"R{rl}",
                             "year": year, "rate": rate, "source_file": path.name})
            continue
        # 格式B：逐行 `- R1：x%`
        rm = re.match(r"^\s*[-–]\s*R([1-5])[:：]\s*([-+\d.]+)\s*%?", line)
        if rm and year:
            recs.append({"country": region, "risk_lvl": f"R{rm.group(1)}",
                         "year": year, "rate": parse_number(rm.group(2)),
                         "source_file": path.name})
    return recs


def _region_from_file(fname: str) -> str:
    for k in ("欧洲", "英国", "美国", "香港", "中国"):
        if k in fname:
            return k
    return fname


# ---------------- character（人物） ----------------
def parse_character(path: Path) -> list[dict]:
    """`- 字段：值` 自由列表；产出 entity 字段 + 关系。"""
    fields: dict = {}
    rels: list[tuple[str, str]] = []
    name = path.stem
    for line in _lines(path):
        m = re.match(r"^\s*[-*]\s*([^:：]{1,20})[:：]\s*(.+?)\s*$", line)
        if not m:
            continue
        key, val = m.group(1).strip(), m.group(2).strip()
        if not key or key in ("---", "===="):
            continue
        if key.replace(" ", "") in ("姓名", "与主角的关系", "关系") and val:
            if key.replace(" ", "") == "姓名":
                name = val.split("/")[0].strip()
            rels.append((key, val))
        else:
            fields[key] = val
    return [{"name": name, "fields": fields, "relations": rels, "source_file": path.name}]


# ---------------- timeline（时间线） ----------------
def parse_timeline(path: Path) -> list[dict]:
    """decade 表 `年份|事件|备注`。"""
    lines = _lines(path)
    recs: list[dict] = []
    decade = None
    for line in lines:
        dm = re.match(r"^#+\s*(19\d0|20\d0)s\s*$", line.strip())
        if dm:
            decade = dm.group(1) + "s"
        cells = _split_table_row(line)
        if len(cells) >= 2:
            y = re.match(r"(\d{4})", cells[0])
            if y and cells[1] and "年份" not in cells[0]:
                recs.append({
                    "event_year": int(y.group(1)),
                    "date_str": cells[0].strip(),
                    "title": cells[1].strip(),
                    "note": cells[2].strip() if len(cells) > 2 else None,
                    "decade": decade,
                    "source_file": path.name,
                })
    return recs


# ---------------- stock_tx（股票台账） ----------------
def parse_stock_tx(path: Path) -> list[dict]:
    """`### 基本信息` 常量 + `### 年度明细` 事件表 → 记录。"""
    lines = _lines(path)
    info: dict = {}
    events: list[dict] = []
    section = None
    for line in lines:
        lm = re.match(r"^#+\s*(.*)$", line.strip())
        if lm:
            t = lm.group(1).strip()
            if "基本信息" in t:
                section = "info"
            elif "年度明细" in t:
                section = "detail"
            continue
        if section == "info":
            m = re.match(r"^\s*[-*]\s*([^:：]+)[:：]\s*(.+)$", line.strip())
            if m:
                info[m.group(1).strip()] = m.group(2).strip()
        elif section == "detail":
            cells = _split_table_row(line)
            if len(cells) >= 3 and re.match(r"\d{4}", cells[0]):
                events.append({
                    "date": cells[0].strip(), "code": cells[1].strip(),
                    "event_type": cells[2].strip(),
                    "note": cells[-1].strip() if len(cells) > 4 else None,
                    "source_file": path.name,
                })
    return [{"info": info, "events": events, "source_file": path.name}]


# ---------------- bank（银行台账） ----------------
def parse_bank(path: Path) -> list[dict]:
    """`## 一、…BEF（祖父）` 分币种节 + 流水表 `日期|理由|收入|支出|余额|备注`。"""
    lines = _lines(path)
    out: list[dict] = []
    cur_seg: dict | None = None
    i = 0
    header = ("日期", "理由", "收入", "支出")
    while i < len(lines):
        line = lines[i]
        hm = re.match(r"^##+\s*(.+)$", line.strip())
        if hm:
            title = hm.group(1)
            cur = currency_from(title)
            cur = cur or (cur_seg["currency"] if cur_seg else None)
            cur_seg = {"seg_title": title, "currency": cur, "rows": []}
            out.append(cur_seg)
        else:
            cells = _split_table_row(line)
            if cur_seg and cells and re.match(r"\d{4}[-/－]", cells[0]):
                cur_seg["rows"].append({
                    "date": cells[0].strip(),
                    "reason": cells[1].strip() if len(cells) > 1 else "",
                    "inflow": parse_number(cells[2]) if len(cells) > 2 else None,
                    "outflow": parse_number(cells[3]) if len(cells) > 3 else None,
                    "balance": parse_number(cells[4]) if len(cells) > 4 else None,
                    "note": cells[5].strip() if len(cells) > 5 else None,
                })
        i += 1
    return out