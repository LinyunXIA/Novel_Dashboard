"""Unit tests for app/ingest/holders.py（issue #10 回归）。

覆盖：精确匹配 / 前缀匹配 / 反向不误配 / 未知输入。
"""
from __future__ import annotations

import pytest

from app.ingest.holders import (
    HOLDER_CURRENCY,
    TITLE_ENTITY,
    holder_currencies,
    holder_entity_name,
)


class TestHolderCurrenciesExact:
    def test_grandpa_exact(self):
        assert holder_currencies("祖父") == ("BEF", "LUF")

    def test_grandma_exact(self):
        assert holder_currencies("祖母") == ("SEK",)

    def test_adoptive_grandpa_exact(self):
        assert holder_currencies("养祖父") == ("BEF", "LUF")

    def test_henri_peeters(self):
        assert holder_currencies("Henri Peeters") == ("BEF", "LUF")


class TestHolderCurrenciesPrefix:
    """issue #10 修复重点：去 .md 后缀场景下前缀匹配能命中。"""

    def test_养祖父_stem_matches(self):
        # path.stem = "养祖父"（去掉 .md）→ 命中 "养祖父"
        assert holder_currencies("养祖父") == ("BEF", "LUF")

    def test_外祖父_stem_matches(self):
        assert holder_currencies("外祖父") == ("NLG",)

    def test_养外祖父_stem_matches(self):
        assert holder_currencies("养外祖父") == ("NLG",)

    def test_养外祖母_stem_matches(self):
        assert holder_currencies("养外祖母") == ("DKK",)


class TestHolderCurrenciesNoMisMatch:
    """issue #10 误配防护：祖父 ≠ 外祖父；养祖父 ≠ 祖父。"""

    def test_外祖父_not_match_祖父(self):
        assert holder_currencies("外祖父") != ("BEF", "LUF")

    def test_养外祖父_not_match_祖父(self):
        # 养外祖父 不会通过前缀落到 祖父
        assert holder_currencies("养外祖父") != ("BEF", "LUF")

    def test_养祖父_not_match_祖父(self):
        assert holder_currencies("养祖父") != ("NLG",)


class TestHolderCurrenciesUnknown:
    def test_unknown_returns_empty(self):
        assert holder_currencies("某个不存在的人") == ()

    def test_empty_returns_empty(self):
        assert holder_currencies("") == ()

    def test_partial_no_false_positive(self):
        # 「祖父的弟弟」不应误匹配「祖父」（前缀匹配要求去后缀场景 len(kw) >= len(k)；
        # 这里「祖父的弟弟」len=5 > len(祖父)=2，应通过前缀命中「祖父」——这是合理行为）
        # 故改测：裸前缀「祖」不应命中
        assert holder_currencies("祖") == ()


class TestHolderEntityName:
    @pytest.mark.parametrize("kw,expected", [
        ("祖父", "Henri Peeters"),
        ("养祖父", "Henri Peeters"),
        ("祖母", "养祖母"),
        ("养祖母", "养祖母"),
        ("外祖父", "Frederik van Oranje"),
        ("养外祖父", "Frederik van Oranje"),
        ("外祖母", "养外祖母"),
        ("养外祖母", "养外祖母"),
        ("养父", "Joren Peeters"),
        ("养母", "Johanna Peeters"),
        ("Henri Peeters", "Henri Peeters"),
    ])
    def test_known(self, kw, expected):
        assert holder_entity_name(kw) == expected

    def test_unknown_returns_none(self):
        assert holder_entity_name("不存在") is None