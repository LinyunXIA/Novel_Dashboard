"""Phase 1 持有人 → 币种 / 实体映射（CLAUDE.md 币种铁律 + 实体范围）。

匹配用「精确的登记名优先」：避免 养外祖母 ≠ 祖母、养外祖父 ≠ 祖父 误配。
"""
from __future__ import annotations

# 精确登记名 → 币种组（铁律：祖父BEF+LUF、祖母SEK、外祖父NLG、外祖母DKK、养父母BEF）
HOLDER_CURRENCY: dict[str, tuple[str, ...]] = {
    "Henri Peeters": ("BEF", "LUF"),
    "养祖父": ("BEF", "LUF"),
    "祖父": ("BEF", "LUF"),
    "养祖母": ("SEK",),
    "祖母": ("SEK",),
    "Frederik van Oranje": ("NLG",),
    "养外祖父": ("NLG",),
    "外祖父": ("NLG",),
    "养外祖母": ("DKK",),
    "外祖母": ("DKK",),
    "养父": ("BEF",),
    "养母": ("BEF",),
}

# 中文职称 → 规范 entity.name
TITLE_ENTITY: dict[str, str] = {
    "Henri Peeters": "Henri Peeters",
    "养祖父": "Henri Peeters",
    "祖父": "Henri Peeters",
    "养祖母": "养祖母",
    "祖母": "养祖母",
    "Frederik van Oranje": "Frederik van Oranje",
    "养外祖父": "Frederik van Oranje",
    "外祖父": "Frederik van Oranje",
    "养外祖母": "养外祖母",
    "外祖母": "养外祖母",
    "养父": "Joren Peeters",
    "养母": "Johanna Peeters",
}


def holder_currencies(keyword: str) -> tuple[str, ...]:
    """按持有人登记名匹配币种组；规则（顺序敏感，先精确后前缀）：

    1. 精确等于登记名 → 命中（最快路径，避免「祖父」误匹配「外祖父」）。
    2. 否则若 kw 以 k 开头且 k 与 kw 等长（去 .md 后缀场景：养外祖父.md → 养外祖父）→ 命中。

    未知返回空（由调用方决定是否进 ingest_report 标需人工）。
    """
    kw = keyword.strip()
    for k, curs in HOLDER_CURRENCY.items():
        # 精确命中：避免「祖父」误吞「外祖父」「养祖父」
        if kw == k:
            return curs
    # 前缀命中：kw 长度 ≥ k（去后缀场景下两者等长或 kw 更长，因 holder 是 path.stem）
    for k, curs in HOLDER_CURRENCY.items():
        if kw.startswith(k) and len(kw) >= len(k):
            return curs
    return ()


def holder_entity_name(keyword: str) -> str | None:
    """按持有人登记名匹配规范 entity.name；规则与 holder_currencies 一致。"""
    kw = keyword.strip()
    # 精确命中优先
    for k, v in TITLE_ENTITY.items():
        if kw == k:
            return v
    # 前缀命中：去后缀场景（path.stem）
    for k, v in TITLE_ENTITY.items():
        if kw.startswith(k) and len(kw) >= len(k):
            return v
    return None