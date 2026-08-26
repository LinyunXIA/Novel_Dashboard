"""通用文本工具（DESIGN §6.2）。

数字/货币/日期 解析，供各 parser 复用。日期默认规则(F)在 resolve_date 实现。
"""
from __future__ import annotations

import re
from datetime import date

# ---- 单位 / 约等 ----
_WAN_RE = re.compile(r"(?<=\d)([万亿])(?![a-zA-Z])")
_APPROX_RE = re.compile(r"[≈~]?\s*")


def parse_number(raw: object, scale: float = 1.0) -> float | None:
    """把 '12,345' '1.2万' '≈4,047.30' 解析为 float；失败返回 None。

    scale 用于把源单位折成统一基底。万/亿 后缀自动乘基数（万=1e4、亿=1e8，
    符合 DESIGN §6.2 万/亿 单位）；不支持单位 → None（由调用方进 ingest_report）。
    """
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace(" ", "")
    s = _APPROX_RE.sub("", s)
    if not s or s in ("-", "--", "—", ""):
        return None
    mult = 1.0
    m = _WAN_RE.search(s)
    if m:
        mult = 10_000.0 if m.group(1) == "万" else 100_000_000.0
        s = (s[:m.start()] + s[m.end():]).strip()
    try:
        return float(s) * scale * mult
    except ValueError:
        return None


def parse_amount(raw: object, unit: str | None = None) -> float | None:
    """金额解析：去千分位、去单位后缀，返回数值（不含单位）。

    issue #132 复核：本函数无生产调用方但被测试钉定，保留为工具函数；
    注意其万/亿仅剥离不换算——需要数量级换算请用 parse_number。
    """
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace(" ", "").replace("≈", "").replace("~", "")
    # issue #32：长词单位（万美金/万USD/万BEF）置前优先剥离，避免「万」先命中残留「美金」
    # 导致 float 失败返 None；补 `美金` 兜底（无「万」前缀的「X 美金」同样被清）。
    s = re.sub(r"(万美金|万USD|万BEF|万|亿|USD|US|BEF|LUF|NLG|DKK|SEK|HKD|EUR|法郎|美元|美金|元)", "", s)
    if not s or s in ("-", "—"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---- 货币 ----
# 单词边界避免「USD」被「US」前缀吞；移除 `US` 别名（`USD` 优先匹配）。
# 顺序无所谓（alternation 最长优先：re 默认从左到右，但 \b 防止前缀误吞）。
# issue #32：补 `CNY`——fx 解析会把 yuan/rmb/人民币 归一为 CNY（_cur），detect_currency
# 口径需一致，否则「人民币」串被识别为空、下游 `cur not in _CURRENCIES` 误回退 BEF。
_CUR_RE = re.compile(r"\b(USD|BEF|LUF|NLG|DKK|SEK|HKD|CNY|EUR|RMB)\b", re.IGNORECASE)


def detect_currency(text: str) -> str | None:
    """从文本(或所在节标题)识别币种后缀；无则 None（由调用方继承节币种）。

    issue #24 顺带修复：旧 regex `(USD|US|BEF|...|EUR|EUR)` 中 `US` 是 `USD` 前缀，
    匹配时优先取 `US`；EUR 又被 dict fallback 到 USD → 纯「EUR」字符串被误判为 USD。
    新 regex 用 \\\\b 单词边界 + 移除 US 别名，所有代码按字面值返回。
    issue #32：补 CNY/RMB——fx 把 yuan/rmb/人民币 归一为 CNY，与 detect 口径对齐。
    返回 `CNY`（RMB 仅是中文习惯写法；下游 `_CURRENCIES` 白名单存 CN/Y 对应 CNY）。
    """
    m = _CUR_RE.search(text or "")
    code = m.group(1).upper() if m else None
    return "CNY" if code == "RMB" else code


_CURRENCIES = ("BEF", "LUF", "NLG", "DKK", "SEK", "USD", "HKD", "CNY", "EUR")


def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    from calendar import monthrange
    return date(year, month, monthrange(year, month)[1])


def resolve_date(year: int, month: int | None = None, day: int | None = None,
                 hint: str | None = None) -> date:
    """DESIGN §6.2 日期默认规则（F）：

    - 仅年份 -> 当年 12-30（注明「年初」-> 01-01）
    - 年+月 -> 该月月底（写明「月初」-> 月 1 日）
    - 上旬/中旬/下旬 -> 1 日 / 11 日 / 21 日
    hint 携带「年初」「月初」「上旬」等标注覆盖默认。
    """
    hint = (hint or "").strip()
    if "年初" in hint or hint in ("年初", "年首"):
        return date(year, 1, 1)
    if day is not None:
        return date(year, month or 12, day)
    if month is not None:
        if "月初" in hint or hint in ("月初", "月初"):
            return date(year, month, 1)
        if "上旬" in hint:
            return date(year, month, 1)
        if "中旬" in hint:
            return date(year, month, 11)
        if "下旬" in hint:
            return date(year, month, 21)
        return _month_end(year, month)
    return date(year, 12, 30)


_DATE_HINT_WORDS = ("年初", "年首", "月初", "上旬", "中旬", "下旬")


def _date_hint(s: str) -> str:
    """从原始日期文本抽提示标注（年初/月初/上中下旬），用于覆盖 resolve_date 默认。"""
    for w in _DATE_HINT_WORDS:
        if w in s:
            return w
    return ""


def parse_date_cell(raw: str) -> tuple[date | None, str | None]:
    """把表格日期格（timeline/bank 复用）解析为 date + 命中的规则名（DESIGN §6.2(F)）。

    支持：YYYY-MM-DD / YYYY-MM / YYYY（及中文分隔 YYYY年M月D日）与 年初/月初/上中下旬
    标注 → 统一走 resolve_date 默认规则。命中 → (date, pattern)；超规则无法解析 →
    尝试用户补充的 date_rule（issue #119），仍失败 → (None, None)，由调用方进
    ingest_report 提示补 date_rule。
    """
    s = (raw or "").strip()
    if not s:
        return None, None
    ym = re.match(r"^(?P<y>[12]\d{3})(?P<rest>.*)$", s)
    if not ym:
        # 年份不在格首（如「约1992年」）：默认规则无从下手，先试用户 date_rule（issue #119）
        hit = apply_user_date_rules(s)
        return hit if hit is not None else (None, None)
    y = int(ym["y"])
    hint = _date_hint(s)
    # 年之后抽数值片段：1 段 → 月（year-month）；≥2 段 → 月+日（year-month-day）。
    # 「年份」单独（rest 无数字）→ 仅年。中文 年/月/日 分隔、'-'、'/'、数字连续均解码。
    parts = re.findall(r"\d+", ym["rest"])
    try:
        if len(parts) >= 2:
            return resolve_date(y, int(parts[0]), int(parts[1])), "year-month-day"
        if parts:
            return resolve_date(y, int(parts[0]), hint=hint), "year-month"
        # 无月日片段：若年份后仍有未识别的残留文字（非分隔符/已知标注），
        # 先尝试用户 date_rule（issue #119）；否则走仅年默认（12-30）。
        leftover = re.sub(r"[\d\s年月度\-/.．]+", "", ym["rest"])
        leftover = re.sub(r"年初|年首|月初|上旬|中旬|下旬", "", leftover)
        if leftover:
            hit = apply_user_date_rules(s)
            if hit is not None:
                return hit
        return resolve_date(y, hint=hint), "year"
    except ValueError:
        pass
    hit = apply_user_date_rules(s)
    return hit if hit is not None else (None, None)


# ---- date_rule 用户补充规则（issue #119 · §6.2「补一条沉淀复用」闭环）----
# 规则存 DB（date_rule 表），ingest 启动 / API 变更时经 load_date_rules 装载；
# pattern 为对原始 cell 全文 re.search 的正则，resolve 为 'MM-DD' 字面，
# 与 cell 内解析出的年份组合成日。规则只在默认规则失配后才尝试。
_USER_DATE_RULES: list[tuple[int, "re.Pattern[str]", str]] = []


def load_date_rules(rows) -> int:
    """装载 (id, pattern, resolve) 序列；非法正则/格式跳过。返回装载条数。"""
    global _USER_DATE_RULES
    out: list[tuple[int, "re.Pattern[str]", str]] = []
    for rid, pattern, resolve in rows or []:
        try:
            compiled = re.compile(str(pattern))
        except re.error:
            continue
        md = re.fullmatch(r"\s*(\d{2})-(\d{2})\s*", str(resolve or ""))
        if not md:
            continue
        out.append((int(rid), compiled, f"{md.group(1)}-{md.group(2)}"))
    _USER_DATE_RULES = out
    return len(out)


def apply_user_date_rules(raw: str) -> tuple[date, str] | None:
    """按用户规则匹配原始 cell（cell 内需含 19xx/20xx 年份，位置不限）；命中返回 (date, 'date_rule:{id}')。"""
    s = (raw or "").strip()
    m4 = re.search(r"(?:19|20)\d{2}", s)
    if not m4:
        return None
    y = int(m4.group(0))
    for rid, pat, mmdd in _USER_DATE_RULES:
        if not pat.search(s):
            continue
        m = re.fullmatch(r"(\d{2})-(\d{2})", mmdd)
        try:
            return date(y, int(m.group(1)), int(m.group(2))), f"date_rule:{rid}"
        except ValueError:
            continue
    return None


# 中文币种词 ↔ 缩写配对（issue #162：节标题常同时出现多个缩写，
# 如 `五、欧元 EUR（2002年BEF+NLG结转）`——配对命中的缩写才可信）。
_ZH_CURRENCY_PAIRS = (
    ("欧元", "EUR"), ("美元", "USD"), ("丹麦克朗", "DKK"), ("瑞典克朗", "SEK"),
    ("比利时法郎", "BEF"), ("卢森堡法郎", "LUF"), ("荷兰盾", "NLG"),
    ("港币", "HKD"), ("港元", "HKD"), ("人民币", "CNY"),
)


def currency_from(seg_title: str) -> str | None:
    """从分节标题（如 `## 一、BEF（祖父）`）抽币种。

    「中文币种词 + 缩写」配对命中优先（issue #162）；无中文词时回退首个缩写扫描
    （兼容 `## 一、BEF（祖父）` 旧式标题）。
    """
    t = seg_title or ""
    for zh, abbr in _ZH_CURRENCY_PAIRS:
        if zh in t and abbr in t.upper():
            return abbr
    for c in _CURRENCIES:
        if c in t.upper():
            return c
    return None


# issue #27：原 parse_relationship 是死代码（无调用方，character 解析走 parse_character
# 内部 KV 收集）。已删除；如未来需要展开，可从 git history 取回。
# issue #31：_THOUSAND_RE 定义未用（千分位剥离在 parse_number/parse_amount 内联实现），已清理。