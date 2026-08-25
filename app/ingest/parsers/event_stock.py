"""事件·股票解析（F-P2-02 · DESIGN §19.6 / §6.3）。

`基准/事件/股票/**` → 归一化 holding_event 记录。best-effort（同 event_movie 风格）：
只认「Style A 流水表」（列头含 `事件类型` + `股数/份额` + `单价` + `金额`），
阶段一只接受 **USD** 金额表（列头如 `金额(万美金)`），快手/香港/英国（万港元/万英镑）显式跳过。

单个事件行 event_type 归一化：买入→`buy` / 卖出→`sell` / 派息分红→`dividend` / 被动抬升·年度
市值·稀释→`pseudo`。**只有 `buy` 行会自动落 holding_event 成本批次**（供 FIFO/市值）；`sell`
需账户结算、`dividend`/`pseudo` 落行会污染 open 批次，故解析返回供 UI/数据调整员按需处理，
不自动写入（对应 DESIGN「导入不关联账户→UI 同币种手动关联补 ledger」）。
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from app.ingest.parsers import _split_table_row
from app.ingest.normalize import parse_number

# 事件词 → 归一化 event_type
_BUY_KW = ("投资", "买入", "认购", "建仓", "出资", "增持", "持股", "获配", "吃下", "收购", "买")
_SELL_KW = ("卖出", "减持", "抛售", "售出", "清仓", "套现")
_DIV_KW = ("派息", "分红", "股息", "红利")


def _norm_type(word: str) -> str:
    if any(k in word for k in _SELL_KW):
        return "sell"
    if any(k in word for k in _DIV_KW):
        return "dividend"
    if any(k in word for k in _BUY_KW):
        return "buy"
    return "pseudo"


def _date_of(cell: str) -> date | None:
    """支持 '2025.12.31' / '2017-05-17' / '2017年5月' / '2018' → date。"""
    m = re.search(r"((?:19|20)\d{2})[-.](\d{1,2})[-.](\d{1,2})", cell)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"((?:19|20)\d{2})年?\s*(\d{1,2})?月?", cell)
    if m:
        return date(int(m.group(1)), int(m.group(2) or 1), 1)
    m = re.search(r"((?:19|20)\d{2})", cell)
    if m:
        return date(int(m.group(1)), 1, 1)
    return None


def _shares_scale(header_cell: str) -> float:
    """股数列头单位：亿股→1e8，万股→1e4，否则 1（普通股/ADS）。"""
    if "亿" in header_cell:
        return 1e8
    if "万" in header_cell:
        return 1e4
    return 1.0


def _is_usd_amount_header(header_cell: str) -> bool:
    """金额列头是否为 USD（阶段一仅 USD）。排除 港元/英镑/人民币/欧元。"""
    if any(u in header_cell for u in ("港", "英", "人", "欧")):
        return False
    return ("万" in header_cell or "USD" in header_cell
            or "美元" in header_cell or "美金" in header_cell)


def _find_style_a_tables(text: str) -> list[dict]:
    """定位 Style A 表。返回 [{header_idx, cols:{date/company/type/shares/unit_price/amount/pct→idx}, shares_scale}]。"""
    lines = text.splitlines()
    tables: list[dict] = []
    for i, line in enumerate(lines[:-1]):
        cells = _split_table_row(line)
        nxt = _split_table_row(lines[i + 1])
        is_sep = (len(nxt) >= 2 and all(re.fullmatch(r":?-{2,}:?", (c or "-")) for c in nxt[:max(2, len(cells))]))
        if not cells or not is_sep or "事件类型" not in "".join(cells):
            continue
        amt_idx = next((k for k, c in enumerate(cells) if "金额" in c), None)
        if amt_idx is None or not _is_usd_amount_header(cells[amt_idx]):
            continue
        def idx(*needles):
            for k, c in enumerate(cells):
                if any(x in c for x in needles):
                    return k
            return None
        shares_idx = idx("股数", "份额")
        tables.append({
            "header_idx": i,
            "cols": {
                "date": idx("日期", "年份", "时间"),
                "company": idx("标的", "代码", "公司", "名字"),
                "type": idx("事件类型"),
                "shares": shares_idx,
                "unit_price": idx("单价"),
                "amount": amt_idx,
                "pct": idx("持股比例", "比例"),
            },
            "shares_scale": _shares_scale(cells[shares_idx]) if shares_idx is not None else 1.0,
        })
    return tables


def _to_company(cell: str) -> tuple[str, str | None]:
    """标的格 → (company, ticker)。'HUYA（虎牙）'→('虎牙','HUYA')；'快手-W 01024.HK'→('快手','01024.HK')。"""
    s = cell.strip().lstrip("0123456789.、 ")
    # 中英文混排：英文代码（…）+ 中文名（…）→ (中文, 代码)
    m = re.match(r"([A-Za-z0-9.\- ]+)[（(]([^（）()]*)[)）]", s)
    if m:
        return m.group(2).strip(), m.group(1).strip()
    # 中文 → 代码：'快手-W 01024.HK'
    m2 = re.match(r"([A-Za-z一-鿿]+)(?:-W)?[ \t]+([A-Z0-9.]{2,})", s)
    if m2:
        return m2.group(1), m2.group(2)
    # 纯中文名
    m3 = re.match(r"([一-鿿A-Za-z0-9]{2,})", s)
    if m3:
        return m3.group(1), None
    raise ValueError("无法解析标的")


def parse_event_stock(path: Path) -> list[dict]:
    """扫描文件的 Style A USD 流水表，逐行归一化为 holding_event 记录。

    返回记录字段：`{company, ticker, date, event_type, shares, unit_price,
    amount(万USD), pct, source_file}`。解析失败的杂行/散文跳过（best-effort）。
    """
    text = path.read_text(encoding="utf-8")
    recs: list[dict] = []
    src = str(path)
    lines = text.splitlines()
    for table in _find_style_a_tables(text):
        cols, scale = table["cols"], table["shares_scale"]
        for line in lines[table["header_idx"] + 2:]:
            cells = _split_table_row(line)
            if len(cells) < 3:
                continue
            t_col = cols["type"]
            raw_type = cells[t_col].strip() if t_col is not None and t_col < len(cells) else ""
            if not raw_type:
                continue
            dt_col = cols["date"]
            if dt_col is None or dt_col >= len(cells):
                continue
            dt = _date_of(cells[dt_col])
            if dt is None:
                continue
            try:
                company, ticker = _to_company(cells[cols["company"]])
            except (ValueError, IndexError, TypeError):
                company, ticker = "未知", None
            shares = None
            if cols["shares"] is not None and cols["shares"] < len(cells):
                v = parse_number(cells[cols["shares"]])
                shares = v * scale if v is not None else None
            unit_price = None
            if cols["unit_price"] is not None and cols["unit_price"] < len(cells):
                unit_price = parse_number(cells[cols["unit_price"]])
            amount = None
            if cols["amount"] < len(cells):
                amount = parse_number(cells[cols["amount"]])
            pct = None
            if cols["pct"] is not None and cols["pct"] < len(cells):
                pm = re.match(r"([\d.]+)%", cells[cols["pct"]])
                pct = float(pm.group(1)) if pm else None
            recs.append({
                "company": company, "ticker": ticker, "date": dt.isoformat(),
                "event_type": _norm_type(raw_type), "shares": shares,
                "unit_price": unit_price, "amount": amount, "pct": pct,
                "source_file": src,
            })
    return recs


# DESIGN §3 要求的统一入口名 parse
parse = parse_event_stock