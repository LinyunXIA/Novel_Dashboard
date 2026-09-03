"""基础解析器（DESIGN §6.3）：bank / stock_tx / return_table / fx / character / timeline。

每个 parse(path) -> list[dict] 归一化记录；失败抛 ParseError 由上层进 ingest_report。
F-P0-02：基础形态解析；收益/初始资产/薪资/支出的细粒度挂账在 F-P0-04/05/06。
"""
from __future__ import annotations

from pathlib import Path
import re

from app.ingest.normalize import parse_number, resolve_date, currency_from, parse_date_cell


class ParseError(Exception):
    """单文件解析失败；进入 ingest_report 需人工处理。"""


# issue #24：salary/household_expense 共用的币种白名单（与 detect_currency 输出口径一致）
_CURRENCIES = ("BEF", "LUF", "NLG", "DKK", "SEK", "USD", "HKD", "CNY", "EUR")


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


def _year_for_line(s: str, fallback: int | None) -> int | None:
    """行内年份优先（issue #25）：19xx/20xx token 单独成词；无则 fallback；都无则 None。

    fallback 形参由 parse_fx 提供文件级推断（首个 4 位 token 或文件名年份），
    自身不读文件，避免与 _detect_year_in_lines 的文件级扫描职责重叠。
    """
    fm = re.search(r"\b(?:19|20)\d{2}\b", s)
    return int(fm.group(0)) if fm else fallback


def parse_fx(path: Path) -> tuple[list[dict], list[str]]:
    """支持两种格式（DESIGN §3）：

    - `1EUR=40.3399BEF` / `1 美元 = 8.2789 元人民币`（格式二）
    - TSV 表 `Currency  Code  Rate(Jan-1995)`，Rate = 1 USD 兑该币 → USD→{Currency}

    行内年份优先于文件头（issue #25）：跨年文件（如 `1999-2002.md`）每行独立定年；
    行内无 token 时回退到文件级（首个 4 位 token 或文件名年份）；
    全无则 year=NULL（基准常量折算）。

    返回 (records, warnings)：文件含 markdown 表格却解析出 0 条时告警
    （issue #115 连锁：新表头曾静默产出 0 条，数据调整员无从察觉）。
    """
    recs: list[dict] = []
    lines = _lines(path)
    # 格式三：中文币种名宽表（列=币种，行=年份；如「所有的货币兑换美金.md」）
    wide = _parse_fx_wide_table(lines)
    if wide:
        return wide
    # 文件级 fallback：行内/节内无年份时使用（issue #25：行/节上下文优先于文件头）
    file_year = _detect_year_in_lines(lines) or _year_from_name(path.name)
    current_year: int | None = file_year
    warnings: list[str] = []
    for line in lines:
        s = line.strip()
        # 节标题型：纯 4 位年份 token（如 `1999`），更新 current_year 并跳过
        if re.fullmatch(r"\d{4}", s):
            current_year = int(s)
            continue
        # 行内年份优先于 current_year（issue #25）
        line_year = _year_for_line(s, current_year)
        # 格式一：1XXX = rate YYY
        m = re.search(r"1\s*([A-Za-z]{2,4})\s*=\s*([\d.,]+)\s*([A-Za-z一-鿿]{2,6})", s, re.I)
        if m:
            fx_to = _cur(m.group(3))
            if fx_to is None:   # 四轮审计 #168：未知币名跳过（宁缺勿错）
                continue
            rate = parse_number(m.group(2))
            if rate is not None and rate <= 0:
                # 七轮审计 #186：非正汇率行不入库并告警（防遮蔽同对 year=NULL 基准常量）
                warnings.append(f"汇率行非正（{m.group(1)}={rate} {fx_to}）已跳过")
                continue
            recs.append({
                "fx_from": m.group(1).upper(), "rate": rate,
                "fx_to": fx_to, "year": line_year,
            })
            continue
        # 格式二：TSV 表格行  `Belgian Franc  BEF  32.14`
        t = re.fullmatch(r"([A-Za-z一-鿿]+(?:\s+[A-Za-z一-鿿]+)?)\s+([A-Z]{3})\s+([\d.,]+)", s)
        if t:
            # 四轮审计 #168：第二列本就是 ISO 三字母代码，直接采信（_cur 识别不出时不再兜底整串大写）
            rate2 = parse_number(t.group(3))
            if rate2 is not None and rate2 <= 0:
                warnings.append(f"汇率行非正（USD/{t.group(2)}={rate2}）已跳过")
                continue
            recs.append({"fx_from": "USD", "rate": rate2,
                         "fx_to": t.group(2).upper(), "year": line_year})
    if not recs and any(l.strip().startswith("|") for l in lines):
        return [], [f"汇率文件含 markdown 表格但解析出 0 条记录（表头格式不识别？）"] + warnings
    return recs, warnings


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
    # 括号内 ISO 码兜底（issue：新表头形如「瑞典克朗(SEK)」「丹麦克朗(DKK)」）
    m = re.search(r"\(\s*([A-Za-z]{3})\s*\)", name)
    if m:
        up = m.group(1).upper()
        if up in ("USD", "EUR", "BEF", "LUF", "NLG", "DKK", "SEK", "HKD",
                  "CNY", "NOK", "JPY", "GBP"):
            return up
    for k, v in _CN_CURRENCY.items():
        if k in name:
            return v
    return None


def _parse_fx_wide_table(lines: list[str]) -> list[dict]:
    """格式三：宽表，表头含「年份」(或 Year) + 币种列，数据行 = 年份 + 各币种兑USD汇率。

    列名支持中文币种名与「名称(CODE)」括号码两种写法；值 `-` 表示该年无数据。
    """
    header: list[str] = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not s.startswith("|"):
            continue
        # 表头识别：中文「年份」或英文「Year」（issue：新版权威汇率表表头）
        is_header = "年份" in s or bool(re.search(r"\|\s*year\s*\|", s, re.I))
        if not is_header:
            continue
        hl = [c.strip() for c in s.strip("|").split("|")] or ["年份"]
        if any(h in _FX_WIDE_HEADERS or _cn_cur(h) for h in hl):
            header = hl
            # 从 i+1 起扫数据行
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


def _cur(name: str) -> str | None:
    """币种名/代码 → 标准代码；无法识别返回 None（四轮审计 #168：宁缺勿错，
    此前整串大写原样入库会产出 'SWISS FRANC' 之类的垃圾货币对）。"""
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
    return None


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
def parse_return_table(path: Path) -> list[dict] | tuple[list[dict], list[str]]:
    """支持三类格式：

    - 全球整合文件（issue #214，Markdown 表格型）：`## 一、欧洲市场（…）` 节标题定地区，
      `### x.4 逐年收益率（%）` 表逐年给出 R1-R5——见 `_parse_return_table_global`；
    - 旧分地区文件两种：欧洲/美国/英国=逐行 `- R1：x%`；香港/中国=竖线分隔单行
      `R1：x｜R2：y｜…`。年份来自 `#### 1999（…）` / `## 1999年` 标题。

    四轮审计 #163：旧格式每 (year) 集满 R1-R5 五档即封盘——文末「分阶段复合年化」附录
    （阶段N/全周期行）与末年明细同 year 且重复，集满后一律忽略。
    """
    lines = _lines(path)
    # issue #214：全球五地整合文件（史实版）按 `## x、欧洲市场（…）` 节标题识别分流
    # （地区名+「市场」紧接，避免误吞旧文件「风险分级定义（…资产市场）」类标题）
    if any(re.match(r"^##\s+[一二三四五]、(?:欧洲|英国|美国|香港|中国)市场", ln)
           for ln in lines):
        return _parse_return_table_global(path, lines)
    recs: list[dict] = []
    year = None
    region = _region_from_file(path.name)
    seen: dict[int, set[int]] = {}   # year -> 已入库 risk_lvl 集
    for line in lines:
        # 年份标题（多级 # 均可）
        ym = re.search(r"^#+\s*(?:\d{4}[-–]\d{4}\s+)?(19|20)\d{2}", line)
        if ym:
            fm = re.search(r"(19|20)\d{2}", ym.group(0))
            if fm:
                year = int(fm.group(0))
        # 格式A：竖线分隔 `R1：4.35｜R2：6.12｜R3：8.64｜R4：68.82｜R5：76.35`
        # 七轮审计 #184：明细行不带 %、「分阶段复合年化」附录行带 %——
        # 带 % 的 pair 视为附录数据跳过（防缺档年被复合费率补齐致口径混合）
        vm = re.finditer(r"R([1-5])\s*[:：]\s*([-+]?\d+(?:\.\d+)?)\s*(%?)", line)
        pairs = [(int(m.group(1)), parse_number(m.group(2)))
                 for m in vm if not m.group(3)]
        done = seen.setdefault(year if year is not None else -1, set())
        if pairs and len(pairs) >= 3 and year and len(done) < 5:
            fresh = [(rl, rate) for rl, rate in pairs if rl not in done]
            if fresh:
                for rl, rate in fresh:
                    recs.append({"country": region, "risk_lvl": f"R{rl}",
                                 "year": year, "rate": rate, "source_file": path.name})
                    done.add(rl)
            continue
        # 格式B：逐行 `- R1：x%`
        rm = re.match(r"^\s*[-–]\s*R([1-5])[:：]\s*([-+\d.]+)\s*%?", line)
        if rm and year and int(rm.group(1)) not in done:
            recs.append({"country": region, "risk_lvl": f"R{rm.group(1)}",
                         "year": year, "rate": parse_number(rm.group(2)),
                         "source_file": path.name})
            done.add(int(rm.group(1)))
    if not recs:
        # 四轮审计 #168：对照 fx 零条告警（#115）——收益表 catch-all 兜底类别
        # （detect `基准/收益表/` 目录级匹配）零条时不再静默
        return [], ["收益测算表解析出 0 条记录（年份标题/R 列格式不识别？）"]
    return recs


def _region_from_file(fname: str) -> str:
    for k in ("欧洲", "英国", "美国", "香港", "中国"):
        if k in fname:
            return k
    return fname


# 全球整合文件（issue #214）：R1-R5 列名 → risk_lvl
_RISK_COLS = {f"R{i}": f"R{i}" for i in range(1, 6)}


def _parse_return_table_global(
        path: Path, lines: list[str]) -> tuple[list[dict], list[str]]:
    """全球五地 R1-R5 整合文件（史实版，Markdown 表格型；issue #214）。

    结构：`## 一、欧洲市场（1947–2025）` 节标题定地区（复用 `_region_from_file`
    的关键词匹配，产出 欧洲/英国/美国/香港/中国 与消费方键一致）；每节
    `### x.4 逐年收益率（%）` 为逐年表，表头 `| 年份 | R1 | R2 | R3 | R4 | R5 | 背景 |`，
    数值为百分数、不带 % 符号，末列背景文本忽略。
    仅 x.4 逐年表入库：`### x.5 分阶段复合年化`、`## 六、五地全周期横向对比`、
    `### 0.2 关键年份验证` 等表均不在「逐年收益率」小节内，自然排除。
    """
    recs: list[dict] = []
    warnings: list[str] = []
    region: str | None = None
    in_year_table = False
    col: dict[str, int] | None = None   # R1..R5 -> 列索引
    for line in lines:
        if line.startswith("## "):
            guess = _region_from_file(line)
            region = guess if guess != line else None
            in_year_table = False
            col = None
            continue
        if line.startswith("### "):
            in_year_table = "逐年收益率" in line
            col = None
            continue
        cells = _split_table_row(line)
        if not in_year_table or region is None or not cells:
            continue
        head = cells[0].strip()
        if "R1" in cells:                       # 表头行：按列名定位 R1-R5
            col = {c: i for i, c in enumerate(cells) if c in _RISK_COLS}
            continue
        if re.match(r"^:?-{2,}:?$", head):      # 分隔行 |---|
            continue
        ym = re.match(r"^(19|20)\d{2}$", head)
        if not ym or col is None:
            continue
        year = int(head)
        for rl, idx in sorted(col.items(), key=lambda kv: kv[1]):
            raw = cells[idx].strip() if idx < len(cells) else ""
            rate = parse_number(raw)
            if rate is None:
                warnings.append(f"{region} {year} 年 {rl} 收益率无法解析：{raw!r}")
                continue
            recs.append({"country": region, "risk_lvl": rl, "year": year,
                         "rate": rate, "source_file": path.name})
    if not recs:
        return [], ["收益测算表（全球表格型）解析出 0 条记录（市场节标题/逐年表格式不识别？）"]
    return recs, warnings


# ---------------- character（人物） ----------------
def parse_character(path: Path) -> list[dict]:
    """`- 字段：值` 自由列表；产出 entity 字段 + 关系。

    issue #27 修复：「姓名」不再重复进 rels（它是 name 本身，不是关系）。
    仅「与主角的关系」/「关系」进入 relations 列表。
    """
    fields: dict = {}
    rels: list[tuple[str, str]] = []
    name = path.stem
    display_name: str | None = None
    for line in _lines(path):
        m = re.match(r"^\s*[-*]\s*([^:：]{1,20})[:：]\s*(.+?)\s*$", line)
        if not m:
            continue
        key, val = m.group(1).strip(), m.group(2).strip()
        if not key or key in ("---", "===="):
            continue
        kn = key.replace(" ", "")
        if kn == "姓名" and val:
            name = val.split("/")[0].strip()
        elif kn in ("显示名", "显示名字", "display_name") and val:
            display_name = val.split("/")[0].strip()
        elif kn in ("与主角的关系", "关系") and val:
            rels.append((key, val))
            # #197：主称谓落 entity.fields，供图谱亲缘推理；重导 upsert merge 即回填
            if kn == "与主角的关系" and "与主角的关系" not in fields:
                fields["与主角的关系"] = val
        else:
            fields[key] = val
    return [{"name": name, "display_name": display_name, "fields": fields,
             "relations": rels, "source_file": path.name}]


# ---------------- timeline（时间线） ----------------
def parse_timeline(path: Path) -> tuple[list[dict], list[str]]:
    """decade 表 `年份|事件|备注`。

    返回：(记录, warnings)。每条事件含 event_year / event_date / title / note /
    decade / source_file。

    issue #8：parse_timeline 解析后无 writer 落库；这里额外解析日期作 event_date
    （用于 H1 时间线对齐与日历游标快照）。
    issue #19：日期统一走 normalize.parse_date_cell（内部 resolve_date 默认规则 F）；
    超规则日期 → 回退当年默认 + warnings 提示补 date_rule。
    """
    lines = _lines(path)
    recs: list[dict] = []
    warnings: list[str] = []
    for line in lines:
        cells = _split_table_row(line)
        if len(cells) >= 2:
            # 首个表格格任一位置含年份即视为事件行（允许「约1992年」等日期格）
            y = re.search(r"(?:19|20)\d{2}", cells[0])
            if y and cells[1] and "年份" not in cells[0]:
                ds = cells[0].strip()
                d, _rule = parse_date_cell(ds)
                if d is None:
                    d = resolve_date(int(y.group(0)))  # 超规则 → 当年默认(12-30)
                    warnings.append(
                        f"时间线日期无法按 §6.2 解析「{ds}」，回退 {d.isoformat()}；补 date_rule：POST /api/v1/date-rules（pattern=正则, resolve='MM-DD'）后重导")
                recs.append({
                    "event_year": d.year,
                    "event_date": d,
                    "date_str": ds,
                    "title": cells[1].strip(),
                    "note": cells[2].strip() if len(cells) > 2 else None,
                    # 四轮审计 #168：decade 按**行年份**推导（节标题可能跨年，
                    # 如 `## 1970s` 节下有 1969 行——忠实转抄会产脏标注）
                    "decade": f"{(d.year // 10) * 10}s",
                    "source_file": path.name,
                })
    return recs, warnings


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


# ---------------- basic_income（基本收入.md：五人初始资产逐年收益终值） ----------------
# issue #211：整合取代旧四类配置推导文件（惠民租房 / 祖产股票债券 / 祖父开店 /
# 经营性房产收益）。文件按人分节（## 一~四、…），节内子表（### x.1 股债 /
# x.2 房产 / 4.3 商业）；年份格支持单年（1974）与段（1950–1969，段内每年同值）；
# 金额为文件终值、逐年直入 income_stream（因子 A「文件终值权威」，issue #114）。
_BASIC_HOLDER_RULES: tuple[tuple[str, str], ...] = (
    ("Frederik van Oranje", "Frederik van Oranje"),
    ("养外祖父", "Frederik van Oranje"),
    ("Henri Peeters", "Henri Peeters"),
    ("养外祖母", "养外祖母"),
    ("养祖母", "养祖母"),
)


def _basic_holder(title: str) -> str | None:
    """`## ` 人物节标题 → holder；汇总节/未识别 → None（该节表格跳过）。

    顺序敏感：养外祖母 显式先于 养祖母，避免职称子串误配。
    """
    for kw, holder in _BASIC_HOLDER_RULES:
        if kw in title:
            return holder
    return None


def _year_span(cell: str) -> list[int]:
    """年份格 → 年列表：`1974` → [1974]；`1950–1969`/`1950-1969` → 段内逐年。"""
    cell = cell.strip()
    m = re.match(r"^(\d{4})\s*[–-]\s*(\d{4})$", cell)
    if m:
        return list(range(int(m.group(1)), int(m.group(2)) + 1))
    if re.match(r"^\d{4}$", cell):
        return [int(cell)]
    return []


def _basic_currency(cell: str) -> str | None:
    """币种格 → ISO 码；`NLG/年` 这类后缀由 _cur 子串别名识别。

    `BEF/LUF` 双币并列（Henri 房产表 1974–2001）返回特殊标记——祖父列 BEF、
    先祖列 LUF 由列规格 dual_cur 决定（_cur 不支持斜杠并列双币）。
    """
    raw = cell.strip()
    if "BEF" in raw and "LUF" in raw:
        return "BEF/LUF"
    # `NLG/年` 这类「码/年」后缀：_cur 精确码集合不识别带后缀串（仅别名表恰有
    # "nlg" 子串侥幸命中），统一剥掉 `/年` 再识别。
    return _cur(raw.replace("/年", "").strip())


def _basic_table_specs(header: list[str]) -> tuple[list[dict], int | None, int | None]:
    """按表头列名定位收益列 → (列规格, 币种列下标, 合计列下标)。

    列规格：{idx, stream_type, group_key, label, dual_cur}；dual_cur 仅 Henri
    房产四列表使用（祖父列固定 BEF、先祖列固定 LUF），单币表为 None。
    """
    specs: list[dict] = []
    cur_col = next((i for i, c in enumerate(header)
                    if "货币" in c or "币种" in c), len(header) - 1)
    total_col = next((i for i, c in enumerate(header) if "合计" in c), None)
    is_split = any("祖父" in c or "先祖" in c for c in header)
    for i, c in enumerate(header):
        if "债券收益" in c:
            specs.append({"idx": i, "stream_type": "security", "group_key": "祖产债券",
                          "label": "祖产股票债券 · 债券收益", "dual_cur": None})
        elif "股票收益" in c:
            specs.append({"idx": i, "stream_type": "security", "group_key": "祖产股票",
                          "label": "祖产股票债券 · 股票收益", "dual_cur": None})
        elif "税后落袋" in c:
            specs.append({"idx": i, "stream_type": "shop", "group_key": "祖父开店",
                          "label": "祖父开店 · 合并税后落袋", "dual_cur": None})
        elif "惠民" in c:
            if is_split and ("祖父" in c or "先祖" in c):
                role = "祖父" if "祖父" in c else "先祖"
                region = "比利时" if role == "祖父" else "卢森堡"
                specs.append({"idx": i, "stream_type": "rent",
                              "group_key": f"惠民租房·{role}",
                              "label": f"惠民租房 · {role}（{region}）",
                              "dual_cur": "BEF" if role == "祖父" else "LUF"})
            elif not is_split:
                specs.append({"idx": i, "stream_type": "rent", "group_key": "惠民租房",
                              "label": "惠民租房", "dual_cur": None})
        elif "经营性" in c:
            if is_split and ("祖父" in c or "先祖" in c):
                role = "祖父" if "祖父" in c else "先祖"
                region = "比利时" if role == "祖父" else "卢森堡"
                specs.append({"idx": i, "stream_type": "property",
                              "group_key": f"经营性房产·{role}",
                              "label": f"经营性房产 · {role}（{region}）",
                              "dual_cur": "BEF" if role == "祖父" else "LUF"})
            elif not is_split:
                specs.append({"idx": i, "stream_type": "property",
                              "group_key": "经营性房产", "label": "经营性房产",
                              "dual_cur": None})
    return specs, cur_col, total_col


def parse_basic_income(path: Path) -> tuple[list[dict], list[str]]:
    """基本收入.md → 逐年收益记录（issue #211，取代旧四类 income_* 文件）。

    记录字段：holder / stream_type(security|rent|property|shop) / group_key /
    label / currency / year / amount / source_line。0 值不产记录（issue #28
    零值跳过纪律，惠民租房 2008 起记 0 即自然无行）。`## 五、汇总` 节跳过；
    `合计` 列作对账校验（分量和 ≠ 合计，容差 1 → warning）。
    """
    lines = _lines(path)
    recs: list[dict] = []
    warnings: list[str] = []
    holder: str | None = None
    specs: list[dict] | None = None
    cur_col: int | None = None
    total_col: int | None = None

    for lineno, line in enumerate(lines, start=1):
        if line.startswith("## "):
            holder = _basic_holder(line)
            specs = None
            continue
        cells = _split_table_row(line)
        if not cells:
            specs = None
            continue
        if holder is None:
            continue
        # 表头行：首格含「年份」→ 重建列规格
        if "年份" in cells[0]:
            specs, cur_col, total_col = _basic_table_specs(cells)
            if not specs:
                warnings.append(f"{path.name} 第{lineno}行 表头未识别出收益列：{' | '.join(cells)}")
                specs = None
            continue
        if specs is None:
            continue
        years = _year_span(cells[0])
        if not years:
            continue
        raw_cur = (_basic_currency(cells[cur_col])
                   if cur_col is not None and cur_col < len(cells) else None)
        if not raw_cur:
            warnings.append(f"{path.name} 第{lineno}行 币种无法识别"
                            f"（{cells[cur_col] if cur_col is not None and cur_col < len(cells) else '?'}），该行跳过")
            continue
        row_total = (parse_number(cells[total_col])
                     if total_col is not None and total_col < len(cells) else None)
        comp_sum = 0.0
        for spec in specs:
            i = spec["idx"]
            if i >= len(cells):
                continue
            amount = parse_number(cells[i])
            if not amount:
                continue
            cur = spec["dual_cur"] if raw_cur == "BEF/LUF" else raw_cur
            if raw_cur == "BEF/LUF" and not spec["dual_cur"]:
                warnings.append(f"{path.name} 第{lineno}行 双币(BEF/LUF)格遇到非分列规格，该列跳过")
                continue
            comp_sum += amount
            for y in years:
                recs.append({"holder": holder, "stream_type": spec["stream_type"],
                             "group_key": spec["group_key"], "label": spec["label"],
                             "currency": cur, "year": y, "amount": amount,
                             "source_line": lineno})
        if row_total is not None and abs(comp_sum - row_total) > 1.0:
            warnings.append(f"{path.name} 第{lineno}行（{holder} {cells[0].strip()}）"
                            f"合计 {row_total:g} ≠ 分量和 {comp_sum:g}")
    return recs, warnings


# ---------------- salary（薪资收入） ----------------
def parse_salary(path: Path) -> tuple[list[dict], list[str]]:
    """逐年薪资台账：`年份 | … | 税后收入 | … | 币种`。

    归属：养父的薪资 → 养父；养母的薪资 → 养母。取文件税后值，系统不重算。

    issue #24 修复：
    - 按表头列名定位「税后」列，不再取「最后一个数字」（避免备注列含数字误采）
    - 币种识别走 detect_currency，兼容「BEF（法郎）」等带后缀写法
    - 表头缺失 / 金额解析失败 → warning 进 ingest_report（parse.py _call 统一收）

    issue #220：币种列名改包含匹配（「结算币种」等带前缀写法同样定位）——养父薪资
    表头为「结算币种」，此前精确匹配失败、currency_col=None，1989 起美国/中国段
    USD/CNY 全部回退默认 BEF 入库。CNY 修正版中国段（CNY）由此正确识别。

    返回 (records, warnings)。
    """
    from app.ingest.normalize import detect_currency
    lines = _lines(path)
    holder = "养母" if "养母" in path.stem else ("养父" if "养父" in path.stem else path.stem)
    recs: list[dict] = []
    warns: list[str] = []

    # 找表头行：首格含「年份」且某列含「税后」
    # 币种列匹配两类：①列名含币种 token（BEF/USD…）
    #                ②列名是「币种」/「货币」/「currency」语义标签（数据行才放真币种）
    header_idx = None
    amount_col = None
    currency_col = None
    for i, line in enumerate(lines):
        cells = _split_table_row(line)
        if len(cells) < 2 or "年份" not in cells[0]:
            continue
        for j, c in enumerate(cells):
            if "税后" in c:
                amount_col = j
            elif (detect_currency(c) or c.strip().upper() in _CURRENCIES
                  # issue #220：列名包含匹配——「结算币种」等带前缀写法同样定位
                  # （养父薪资表头为「结算币种」，此前定位失败致全段回退默认 BEF）
                  or "币种" in c or "货币" in c
                  or c.strip().lower() == "currency"):
                currency_col = j
        if amount_col is not None:
            header_idx = i
            break

    if header_idx is None or amount_col is None:
        warns.append(f"{path.name} 找不到含「年份」+「税后」的表头行，整文件跳过")
        return recs, warns

    # 数据行：表头下 1 行可能是分隔行 `|---|`，跳过
    j = header_idx + 1
    if j < len(lines) and _split_table_row(lines[j]) and all(
            not _split_table_row(lines[j])[k].strip()
            for k in range(1, len(_split_table_row(lines[j])))
    ):
        j += 1

    for line in lines[j:]:
        cells = _split_table_row(line)
        if not cells or len(cells) <= amount_col:
            continue
        if not re.match(r"^\d{4}$", cells[0].strip()):
            continue
        amount = parse_number(cells[amount_col])
        if amount is None:
            warns.append(f"{path.name} {cells[0]} 年 {cells[amount_col]!r} 金额解析失败，跳过")
            continue
        # 币种：定位列 → detect_currency；未识别则默认 BEF（养父/养母薪资历史币种）
        cur = None
        if currency_col is not None and currency_col < len(cells):
            cur = detect_currency(cells[currency_col]) or cells[currency_col].strip().upper()
        if cur not in _CURRENCIES:
            cur = "BEF"
        recs.append({"holder": holder, "year": int(cells[0]),
                     "currency": cur, "after_tax": amount})

    # issue #222：表外「退职金专项核算」段——退休年一次性税后退职金（比利时 Assigned out
    # 口径：法律雇主 IBM 比利时，2 倍基薪、EUR 计价、18% 优惠税率）。逐年表不含此行；
    # 取「税后退职金」行 **bold** 段内金额+币种（bold 优先——同行另有税前数，
    # 如「893,690 × (1-18%) = **732,826 EUR**」），年份取退职金段标题中的 4 位年。
    sev_year = None
    for line in lines:
        if line.lstrip().startswith("#") and "退职金" in line:
            ym = re.search(r"(\d{4})", line)
            if ym:
                sev_year = int(ym.group(1))
    for line in lines:
        if "税后退职金" not in line:
            continue
        m = re.search(r"\*\*\s*([\d,]+(?:\.\d+)?)\s*([A-Za-z]{3})", line)
        if not m:   # 兜底：行末「数字 币种」
            m = re.search(r"([\d,]+(?:\.\d+)?)\s*([A-Za-z]{3})\s*$", line.strip())
        if not m:
            warns.append(f"{path.name} 退职金行金额/币种解析失败：{line.strip()[:60]}")
            continue
        if sev_year is None:
            warns.append(f"{path.name} 退职金段标题缺年份，该行跳过")
            continue
        sev_cur = m.group(2).upper()
        if sev_cur not in _CURRENCIES:
            warns.append(f"{path.name} 退职金币种 {sev_cur} 不在白名单，该行跳过")
            continue
        recs.append({"holder": holder, "year": sev_year, "currency": sev_cur,
                     "after_tax": parse_number(m.group(1)), "component": "severance"})
    return recs, warns


# ---------------- household_expense（家庭支出） ----------------
def parse_household_expense(path: Path) -> tuple[list[dict], list[str]]:
    """`年份 | … | 年度总支出 | … | 币种`；挂 Henri Peeters 账户支出。

    issue #24 修复：
    - 按表头列名定位「年度总支出」列，不再硬取 cells[-1]（避免末列是备注/币种时错采）
    - 币种识别走 detect_currency；硬编码 currency='BEF' 改为按文件识别后回退 BEF
    - 表头缺失 / 金额解析失败 → warning 进 ingest_report

    返回 (records, warnings)。
    """
    from app.ingest.normalize import detect_currency
    lines = _lines(path)
    recs: list[dict] = []
    warns: list[str] = []

    header_idx = None
    amount_col = None
    currency_col = None
    for i, line in enumerate(lines):
        cells = _split_table_row(line)
        if len(cells) < 2 or "年份" not in cells[0]:
            continue
        for j, c in enumerate(cells):
            if "总支出" in c:
                amount_col = j
            elif (detect_currency(c) or c.strip().upper() in _CURRENCIES
                  # issue #220：列名包含匹配——「结算币种」等带前缀写法同样定位
                  # （养父薪资表头为「结算币种」，此前定位失败致全段回退默认 BEF）
                  or "币种" in c or "货币" in c
                  or c.strip().lower() == "currency"):
                currency_col = j
        if amount_col is not None:
            header_idx = i
            break

    if header_idx is None or amount_col is None:
        warns.append(f"{path.name} 找不到含「年份」+「总支出」的表头行，整文件跳过")
        return recs, warns

    j = header_idx + 1
    if j < len(lines) and _split_table_row(lines[j]) and all(
            not _split_table_row(lines[j])[k].strip()
            for k in range(1, len(_split_table_row(lines[j])))
    ):
        j += 1

    for line in lines[j:]:
        cells = _split_table_row(line)
        if not cells or len(cells) <= amount_col:
            continue
        if not re.match(r"^\d{4}$", cells[0].strip()):
            continue
        amount = parse_number(cells[amount_col])
        if amount is None:
            warns.append(f"{path.name} {cells[0]} 年 {cells[amount_col]!r} 金额解析失败，跳过")
            continue
        cur = None
        if currency_col is not None and currency_col < len(cells):
            cur = detect_currency(cells[currency_col]) or cells[currency_col].strip().upper()
        if cur not in _CURRENCIES:
            cur = "BEF"
        recs.append({"holder": "Henri Peeters", "year": int(cells[0]),
                     "amount": amount, "currency": cur, "source_file": path.name})
    return recs, warns


# ---------------- bank（银行台账） ----------------
_BANK_HOLDER_HINTS = (
    "Henri Peeters", "Joren Peeters", "Johanna Peeters",
    "祖父", "祖母", "外祖父", "外祖母", "养父", "养母",
)


def _extract_holder_from_title(title: str, fallback: str | None = None) -> str | None:
    """从节标题 / 文件名抽持有人**规范化 entity.name**（issue #9：补 entity 归属）。

    命中后通过 TITLE_ENTITY 映射回规范 entity.name（如「祖父」→「Henri Peeters」、
    「外祖父」→「Frederik van Oranje」），确保 account 唯一键一致。
    """
    from app.ingest.holders import TITLE_ENTITY
    # 精确命中
    if title in TITLE_ENTITY:
        return TITLE_ENTITY[title]
    # 子串命中：按长度倒序避免「祖父」误吞「外祖父」（更长的 key 先匹配）
    for k in sorted(TITLE_ENTITY.keys(), key=len, reverse=True):
        if k in title:
            return TITLE_ENTITY[k]
    # fallback 也走规范映射（如 filename="祖父" → "Henri Peeters"）
    if fallback is not None:
        return TITLE_ENTITY.get(fallback, fallback)
    return None


def _extract_bank_name_from_header(lines: list[str]) -> str | None:
    """从文件头部注释行抽开户行（如 `# 开户行：德意志银行`）。"""
    for line in lines[:20]:
        m = re.search(r"开户行[：:]\s*(.+)", line)
        if m:
            return m.group(1).strip()
    return None


def parse_bank(path: Path) -> tuple[list[dict], list[str]]:
    """`## 一、…BEF（祖父）` 分币种节 + 流水表 `日期|理由|收入|支出|余额|备注`。

    返回：(segments, warnings)。每节 dict（含 seg_title / currency / holder / bank /
    rows）；rows 中每条流水含 date(date 对象) / date_raw / reason / inflow / outflow /
    balance / note。

    issue #9：补持有人解析（节标题「BEF（祖父Henri Peeters注入）」→ 祖父；文件 stem
    `祖父.md` → 祖父；最终回退 holder_entity_name）；补开户行从头部注释抽取。
    issue #19：日期统一走 normalize.parse_date_cell（resolve_date 默认规则 F），非完整
    年月日（YYYY / YYYY-MM / 月初 / 年初 / 上中下旬）不再被整行丢弃；超规则 → 该行跳过
    + warnings 提示补 date_rule。
    """
    from app.ingest.holders import holder_entity_name
    lines = _lines(path)
    out: list[dict] = []
    warnings: list[str] = []
    cur_seg: dict | None = None
    file_holder = holder_entity_name(path.stem)        # 文件名兜底
    bank_name = _extract_bank_name_from_header(lines)
    i = 0
    while i < len(lines):
        line = lines[i]
        hm = re.match(r"^##+\s*(.+)$", line.strip())
        if hm:
            title = hm.group(1)
            cur = currency_from(title)
            seg_holder = _extract_holder_from_title(title, fallback=file_holder)
            cur = cur or (cur_seg["currency"] if cur_seg else None)
            cur_seg = {"seg_title": title, "currency": cur,
                       "holder": seg_holder, "bank": bank_name, "rows": []}
            out.append(cur_seg)
        else:
            cells = _split_table_row(line)
            if cur_seg and cells and re.match(r"\d{4}(?:[-/－]|年|$)", cells[0]):
                d, rule = parse_date_cell(cells[0])
                if d is None:
                    warnings.append(
                        f"银行流水日期无法按 §6.2 解析「{cells[0].strip()}」，该行跳过；补 date_rule：POST /api/v1/date-rules（pattern=正则, resolve='MM-DD'）后重导")
                    i += 1
                    continue
                cur_seg["rows"].append({
                    "date": d,
                    "date_raw": cells[0].strip(),
                    "date_rule": rule,
                    "reason": cells[1].strip() if len(cells) > 1 else "",
                    "inflow": parse_number(cells[2]) if len(cells) > 2 else None,
                    "outflow": parse_number(cells[3]) if len(cells) > 3 else None,
                    "balance": parse_number(cells[4]) if len(cells) > 4 else None,
                    "note": cells[5].strip() if len(cells) > 5 else None,
                })
        i += 1
    return out, warnings