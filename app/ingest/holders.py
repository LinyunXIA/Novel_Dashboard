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
    """按持有人登记名精确匹配币种组；未知返回空。"""
    kw = keyword.strip()
    for k, curs in HOLDER_CURRENCY.items():
        # 精确或「以 k 开头」匹配（如 养外祖父.md -> 养外祖父）
        if kw == k or kw.startswith(k) and len(k) >= len(kw):
            return curs
    return ()


def holder_entity_name(keyword: str) -> str | None:
    kw = keyword.strip()
    for k, v in TITLE_ENTITY.items():
        if kw == k or (kw.startswith(k) and len(k) >= len(kw)):
            return v
    return None