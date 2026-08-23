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
    lines = _lines(path)
    # 格式三：中文币种名宽表（列=币种，行=年份；如「所有的货币兑换美金.md」）
    wide = _parse_fx_wide_table(lines)
    if wide:
        return wide
    cur_year = _detect_year_in_lines(lines) or _year_from_name(path.name)
    for line in lines:
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


_CN_CURRENCY = {
    "比利时法郎": "BEF", "荷兰盾": "NLG", "卢森堡法郎": "LUF",
    "挪威克朗": "NOK", "丹麦克朗": "DKK", "港币": "HKD",
    "人民币": "CNY", "日元": "JPY", "欧元": "EUR", "美元": "USD",
    "瑞典克朗": "SEK", "英镑": "GBP",
}

_FX_WIDE_HEADERS = ("比利时法郎", "荷兰盾", "卢森堡法郎", "丹麦克朗", "港币", "人民币", "欧元")


def _cn_cur(name: str) -> str | None:
    name = name.strip()
    if not name:
        return None
    for k, v in _CN_CURRENCY.items():
        if k in name:
            return v
    return None


def _parse_fx_wide_table(lines: list[str]) -> list[dict]:
    """格式三：宽表，表头含「年份」+中文币种名，数据行 = 年份 + 各币种兑USD汇率。

    语义：rate = 1 USD 兑换该币 → fx_from='USD', fx_to=<ver币种>，year=该行年份。
    值 `-` 表示该年无数据（不产出记录）。
    """
    header: list[str] = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not s.startswith("|") or "年份" not in s:
            continue
        hl = [c.strip() for c in s.strip("|").split("|")] or ["年份"]
        if any(h in _FX_WIDE_HEADERS for h in hl):
            header = hl
            # 从 i+1 起扫数据行
            cur_year = None
            recs: list[dict] = []
            for data_line in lines[i + 1:]:
                ds = data_line.strip()
                if not ds.startswith("|"):
                    continue
                cells = [c.strip() for c in ds.strip("|").split("|")]
                if not cells or not re.fullmatch(r"\d{4}", cells[0]):
                    continue
                year = int(cells[0])
                for col, value in enumerate(header[1:], start=1):
                    if col >= len(cells):
                        break
                    code = _cn_cur(value)
                    if not code or cells[col] in ("-", "—", ""):
                        continue
                    rate = parse_number(cells[col])
                    if rate is not None:
                        recs.append({"fx_from": "USD", "rate": rate,
                                     "fx_to": code, "year": year})
            return recs
    return []


def _cur(name: str) -> str:
    """币种名/代码 → 标准代码；无法识别返回大写原样。"""
    n = name.strip().lower()
    up = n.upper()
    if up in ("USD", "EUR", "BEF", "LUF", "NLG", "DKK", "SEK", "HKD", "CNY"):
        return up
    alias = {"bf": "BEF", "belgian": "BEF", "gulden": "NLG", "guilder": "NLG",
             "guiden": "NLG", "nlg": "NLG", "法郎": "BEF", "比利时法郎": "BEF",
             "荷兰盾": "NLG", "瑞典克朗": "SEK", "丹麦克朗": "DKK", "美元": "USD"}
    for k, v in alias.items():
        if k in n:
            return v
    for k, v in _CUR_NAME.items():
        if k in n:
            return v
    return up


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


# ---------------- initial_asset（初始资产） ----------------
def parse_initial_asset(path: Path) -> list[dict]:
    """`- 现金：值 币种` 同行 / `- 债券:`+子项面值 / `- 股票(说明)`+编号持仓% / `- 房产`+子项。

    产出存量记录；币种由持有人推断或文本识别（F-P0-04）。
    """
    from app.ingest.holders import holder_currencies, holder_entity_name
    lines = _lines(path)
    holder = path.stem
    curs = holder_currencies(holder)
    entity = holder_entity_name(holder) or holder
    defcur = curs[0] if curs else None
    recs: list[dict] = []
    section: str | None = None
    idx = 0

    def add(at: str, name: str, currency: str | None = None, face=None, pct=None, gk=None):
        nonlocal idx
        recs.append({"entity_name": entity, "asset_type": at, "name": name,
                     "currency": currency or defcur, "face_value": face, "pct": pct,
                     "group_key": gk, "idx": idx})
        idx += 1

    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or s.startswith(">"):
            continue
        # 标题（含括号说明）：`- 债券:` / `- 股票（…）` / `- 房产` / `- 现金：…`
        th = re.match(r"[-\*]\s*(现金|债券|股票|房产)", s)
        if th:
            section = th.group(1)
        if section == "现金":
            m = re.match(r"[-\*]\s*现金\s*[:：]\s*([\d,.\s]+)\s*([A-Za-z一-鿿]{2,8})", s)
            if m:
                add("cash", "初始现金", _cur(m.group(2)) if _cur(m.group(2)) != m.group(2).upper() else defcur,
                    face=parse_number(m.group(1)))
                continue
        if section == "债券":
            m = re.match(r"[-\*]\s*债券[A-Za-z]?\s*[:：]\s*(.*?)（?面值\s*([\d,.\s]+)\s*([A-Za-z一-鿿]{2,8})?", s)
            if m:
                add("bond", (m.group(1) or "").strip() or f"债券{idx}", _cur(m.group(3) or "") or defcur,
                    face=parse_number(m.group(2)), gk="祖产股票债券")
                continue
        if section == "股票":
            m = re.match(r"[-\*]?\s*\d+\.\s*(.*?)（?[:：]?\s*最终持仓\s*([\d.]+)%", s)
            if not m:
                m = re.match(r"[-\*]?\s*\d+\.\s*(.*?)[:：].*?([\d.]+)%", s)
            if m:
                add("stock", (m.group(1) or "").strip(), defcur, pct=parse_number(m.group(2)),
                    gk="祖产股票债券")
                continue
        if section == "房产":
            m = re.match(r"[-\*]\s*房产[A-Z]?\s*[:：]\s*(.+)", s)
            if m:
                add("property", m.group(1).strip(), defcur, gk="房产")
                continue
    return recs


# ---------------- income_security（祖产债券收益流） ----------------
_BUND_HOLDER = {"荷兰": "养外祖父", "丹麦": "养外祖母", "瑞典": "养祖母", "比利时": "养祖父", "卢森堡": "养祖父"}


def _bund_holder(country: str) -> str:
    return _BUND_HOLDER.get(country, country)


def _next_line_rate(lines: list[str], idx: int) -> float | None:
    for k in range(idx + 1, min(idx + 4, len(lines))):
        m = re.search(r"票面?\s*固定\s*([\d.]+)\s*%", lines[k])
        if m:
            return parse_number(m.group(1))
    return None


def parse_income_security(path: Path) -> list[dict]:
    """祖产股票债券：国家 × 每券 {面值, 固定票息%。币种}。

    产出"每券"记录；归属 entity + 币种由持有国 → 铁律映射（F-P0-05）。
    逐年票息 = 面值 × 票息（固定费率，全周期不变；由 writer 生成 income_stream）。
    """
    from app.ingest.holders import holder_currencies
    lines = _lines(path)
    recs: list[dict] = []
    country = None
    for idx, line in enumerate(lines):
        cm = re.match(r"^##\s*(荷兰|丹麦|瑞典|比利时|卢森堡)债券", line)
        if cm:
            country = cm.group(1)
            continue
        # 每券：`### N. 名称（面值 X 币种，固定票息Y%）`
        m = re.match(r"^###\s+\d+\.\s*(.+?)（面值\s*([\d,]+)\s*([A-Za-z一-鿿]{2,8})", line)
        if not m or not country:
            continue
        cur_code = m.group(3)
        face_cur = _cur(cur_code) or (holder_currencies(_bund_holder(country))[0] if country else None)
        # 票息：同行（`票面固定X%` / `固定票息X%`）或下行
        rate = None
        rm = re.search(r"(?:票面?\s*固定\s*票息|票面?\s*固定|固定\s*票息)\s*([\d.]+)\s*%", line)
        if rm:
            rate = parse_number(rm.group(1))
        else:
            rate = _next_line_rate(lines, idx)
        recs.append({"country": country, "name": m.group(1).strip(),
                     "face_value": parse_number(m.group(2)), "currency": face_cur,
                     "rate_pct": rate, "holder": _bund_holder(country)})
    return recs


# ---------------- income_rent（惠民租房收益流） ----------------
def parse_income_rent(path: Path) -> list[dict]:
    """分国基础表：国家/持有人/套数/币种/1974单套年租金 → 逐年租金推导配置。

    逐年租金 = 单套年租金 × 套数 × 分段复利(1974-84 +7%, 85-99 +3.5%, 00-07 +5%)。
    """
    lines = _lines(path)
    recs: list[dict] = []
    i = 0
    while i < len(lines):
        cells = _split_table_row(lines[i])
        if len(cells) >= 5 and cells[0] in ("比利时", "荷兰", "丹麦", "瑞典"):
            cur = _cur(cells[3])
            m = re.search(r"([\d.]+)\s*([A-Za-z一-鿿]{2,6})", cells[4])
            recs.append({
                "country": cells[0], "holder": cells[1].strip(), "units": parse_number(cells[2]),
                "currency": cur or _cur(cells[1]), "unit_rent": parse_number(m.group(1)) if m else None,
                "start": 1974,
            })
        i += 1
    return recs


# ---------------- income_property（经营性房产收益流） ----------------
def parse_income_property(path: Path) -> list[dict]:
    """属地 × 房产 × 1974基准年收入(本土货币) → 逐年营收推导配置。

    逐年 = 属地基准 × 分段复利(1974-2007: +7%→+3.5%→+5%（累计≈5.21）; 2008-16 +3%; 17-22 +2.8%; 23-25 +1.5%)。
    归属: 比利时/卢森堡→Henri, 荷兰→养外祖父, 丹麦→养外祖母, 瑞典→养祖母。
    """
    from app.ingest.holders import holder_currencies
    lines = _lines(path)
    recs: list[dict] = []
    for line in lines:
        m = re.match(r"\| (卢森堡|比利时|荷兰|丹麦|瑞典) \| 房产([A-Z]+) \| (.+?)\|.*\| ([\d,]+) ([A-Za-z一-鿿]{2,6}) \|", line)
        if m:
            recs.append({
                "country": m.group(1), "prop": "房产" + m.group(2), "name": m.group(3).strip(),
                "base1974": parse_number(m.group(4)), "currency": _cur(m.group(5)),
                "holder": {"卢森堡": "Henri Peeters", "比利时": "Henri Peeters",
                           "荷兰": "养外祖父", "丹麦": "养外祖母", "瑞典": "养祖母"}[m.group(1)],
            })
    return recs


# ---------------- income_shop（开店收益流） ----------------
def parse_income_shop(path: Path) -> list[dict]:
    """时段表 `时间段 | 货币 | … | 合并税后落袋` → 逐年(时段内取落袋均值)。

    归属：开店挂 Henri Peeters 账户（祖父运营）。遇「跨期对比辅助表」后停止（避免 EUR 折算重复段）。
    """
    lines = _lines(path)
    recs: list[dict] = []
    for line in lines:
        # 仅当"辅助表"作为表格标题（## 或 #）出现才停，避免正文含"跨期对比"字样误触
        if line.strip().startswith("#") and ("跨期对比辅助表" in line or "对比辅助表" in line):
            break
        m = re.match(r"\| (\d{4})[–-](\d{4}) \| (\w+) \| .* \| ([\d.]+) \|", line)
        if m:
            y0, y1, cur, last = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
            recs.append({"holder": "Henri Peeters", "y0": y0, "y1": y1,
                         "currency": _cur(cur) or "BEF", "amount": parse_number(last)})
    return recs


# ---------------- salary（薪资收入） ----------------
def parse_salary(path: Path) -> list[dict]:
    """逐年薪资台账：`年份 | … | 税后总收入 | …`。

    归属：养父的薪资 → 养父；养母的薪资 → 养母。取文件税后值，系统不重算。
    """
    lines = _lines(path)
    holder = "养母" if "养母" in path.stem else ("养父" if "养父" in path.stem else path.stem)
    recs: list[dict] = []
    for line in lines:
        cells = _split_table_row(line)
        if not cells or not re.match(r"^\d{4}$", cells[0].strip()):
            continue
        # 找币种列（标准代码，如 BEF/USD/EUR…）
        cur = next((c.strip().upper() for c in cells
                    if c.strip().upper() in ("BEF", "LUF", "NLG", "DKK", "SEK", "USD", "HKD", "EUR")), None)
        # 税后收入 = 行内最后一个纯数字（工作表最后列之前的税后总收入）
        nums = [parse_number(c) for c in cells]
        last_num = next((v for v in reversed(nums) if v is not None), None)
        recs.append({"holder": holder, "year": int(cells[0]), "currency": cur,
                     "after_tax": last_num, "note": cells[-1] if len(cells) > 1 else None})
    return recs


# ---------------- household_expense（家庭支出） ----------------
def parse_household_expense(path: Path) -> list[dict]:
    """`年份 | … | 年度总支出`；挂 Henri Peeters 账户支出。"""
    lines = _lines(path)
    recs: list[dict] = []
    for line in lines:
        cells = _split_table_row(line)
        if len(cells) >= 2 and re.match(r"^\d{4}$", cells[0].strip()):
            recs.append({"holder": "Henri Peeters", "year": int(cells[0]),
                         "amount": parse_number(cells[-1]), "currency": "BEF"})
    return recs


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