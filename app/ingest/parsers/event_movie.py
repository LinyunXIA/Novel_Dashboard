"""事件·电影解析（F-P2-01 · DESIGN §19.6）。

`基准/事件/电影/*.md` → 归一化 {title, currency, region, investment_total, ...}。
best-effort 正则：解析失败字段留空，原 md 保留供数据调整员补字段。
"""
from __future__ import annotations

import re
from datetime import date as _date
from pathlib import Path


# 万元/亿美元 → 美元整数
_UNIT = {"万": 1e4, "亿": 1e8}


def _to_usd(num: str, unit: str) -> float | None:
    try:
        return float(num) * _UNIT[unit]
    except (ValueError, KeyError):
        return None


def _first_year(text: str, *needles: str) -> int | None:
    for n in needles:
        m = re.search(rf"{n}.*?(\d{{4}})\s*年", text)
        if m:
            return int(m.group(1))
    return None


def parse_event_movie(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    rec: dict = {"source_file": str(path), "currency": "USD"}

    # title: 「# 《X》」或回退文件名
    m = re.search(r"#\s*《(.+?)》", text)
    rec["title"] = m.group(1).strip() if m else path.stem

    # 资金池区域（北美/海外）
    if "北美" in text and "海外" in text:
        rec["region"] = "NA+OS"
    elif "北美" in text:
        rec["region"] = "NA"
    elif "海外" in text:
        rec["region"] = "OS"

    # 投资总额：取「资金池」表里"主角出资金额"——按行簇分（史实总盘 vs 主角出资），取最后簇
    # （8 个文件里 主角出资 都在 史实总盘 之后；用最后簇 = 主角出资总额）
    pools: list[tuple[int, float]] = []   # (line_index, amount_usd)
    for idx, line in enumerate(text.splitlines()):
        m = re.search(r"\|[^|]*?资金池[^|]*?\|[^|]*?(\d+(?:\.\d+)?)\s*(万|亿)", line)
        if m:
            v = _to_usd(m.group(1), m.group(2))
            if v:
                pools.append((idx, v))
    if pools:
        # 分簇：相邻匹配 line 差 ≤3 视为同簇
        clusters: list[list[float]] = [[pools[0][1]]]
        for i in range(1, len(pools)):
            if pools[i][0] - pools[i - 1][0] <= 3:
                clusters[-1].append(pools[i][1])
            else:
                clusters.append([pools[i][1]])
        rec["investment_total"] = sum(clusters[-1])   # 最后簇 = 主角出资

    # 投资日期：找首个建仓/摄制相关年份
    inv_year = _first_year(text, "建仓", "摄制", "开机", "筹备")
    if inv_year:
        rec["investment_date"] = _date(inv_year, 12, 31)

    # 本金返还：日期与金额在文中顺序不一，独立抽取（限本金返还上下文）
    ret_block = re.search(r"本金返还.{0,80}?(\d{4})\s*年\s*(\d{1,2})\s*月.*?本金.*?(\d+(?:\.\d+)?)\s*(万|亿)", text, re.S) \
        or re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月.{0,80}?本金返还", text, re.S)
    if ret_block:
        rec["principal_return_date"] = _date(int(ret_block.group(1)), int(ret_block.group(2)), 1)
        if ret_block.lastindex >= 4 and ret_block.group(3) and ret_block.group(4):
            amt = _to_usd(ret_block.group(3), ret_block.group(4))
            if amt:
                rec["principal_return_amount"] = amt
    # fallback：若上面没匹配到金额（date-only），用「本金…X万」兜底
    if rec.get("principal_return_amount") is None and rec.get("principal_return_date") is not None:
        m = re.search(r"本金[^0-9]*?(\d+(?:\.\d+)?)\s*(万|亿)\s*美", text)
        if m:
            amt = _to_usd(m.group(1), m.group(2))
            if amt:
                rec["principal_return_amount"] = amt

    # 分红总额：取「税前总分红…合计…X亿」合计行（re.S 跨行；非贪婪过子行捕合计）
    m = re.search(r"税前总分红.*?合计.*?(\d+(?:\.\d+)?)\s*(万|亿)", text, re.S)
    if m:
        v = _to_usd(m.group(1), m.group(2))
        if v:
            rec["dividends_total"] = v

    # raw_cashflows（解析明细摘要，供 UI/补字段参考）
    rec["raw_cashflows"] = {
        "investment_total_found": rec.get("investment_total"),
        "principal_return": rec.get("principal_return_amount"),
        "principal_return_date": str(rec.get("principal_return_date")) if rec.get("principal_return_date") else None,
        "dividends_total": rec.get("dividends_total"),
    }
    return [rec]


# DESIGN §3 要求的统一入口名 parse
parse = parse_event_movie