"""收益展开因子配置（issue #69 · DESIGN §6.5 实现注记）。

背景：三类收益文件（惠民租房 / 经营性房产 / 祖产股票债券）的源 md **只含基桩值**
（1974 基准年收入、单套年租金、面值 × 固定票息），没有逐年金额表。系统按文件自身
口径的分段复利，在 ingest 时把基桩值确定性展开为逐年 income_stream——这是
「文件权威值 + 系统按文件规则搬运」，不是黑箱重算；涨幅分段数字与源文件一致，
集中在本模块便于核对与调整（PRD §6.10「系统直接取数入账不重算税率/CPI」的
实现边界：展开所用的分段系数本身即文件内容的一部分）。

数值纪律：
- 修改任何分段率 = 改变全时间链路金额 → 必须回溯 `Design_Folder/时间线.md` 核对下游；
- 因子仅依赖年份，纯函数、无 IO，便于单测。
"""
from __future__ import annotations

# 分段表：(截至年, 年涨幅)。None=开放末端（该段适用到最后）。
# 数值来源：`基准/收益表/经营性房产收益.md` 与 `基准/收益表/惠民租房.md` 的文字说明段。
_PROPERTY_SEGMENTS: tuple[tuple[int | None, float], ...] = (
    (1984, 1.07),     # 1975-84 +7%
    (1999, 1.035),    # 1985-99 +3.5%
    (2007, 1.05),     # 2000-07 +5%
    (2016, 1.03),     # 2008-16 +3%
    (2022, 1.028),    # 2017-22 +2.8%
    (None, 1.015),    # 2023 起 +1.5%
)

_RENT_SEGMENTS: tuple[tuple[int | None, float], ...] = (
    (1984, 1.07),     # 1975-84 +7%
    (1999, 1.035),    # 1985-99 +3.5%
    (None, 1.05),     # 2000 起 +5%
)

# 祖产债券票息展开窗口（issue #69：由硬编码参数化为配置默认；源文件无期限列，
# 全周期固定票息，窗口即「持有至观察期末」口径）
SECURITY_DEFAULT_YEARS: tuple[int, int] = (1947, 2025)

# 各收益流的基桩年（base year：该年系数=1，自次年始复利）
_BASE_YEAR = 1974


def compound_factor(year: int, segments: tuple[tuple[int | None, float], ...],
                    base_year: int = _BASE_YEAR) -> float:
    """基桩年到目标年的分段复利系数；year <= base_year → 1.0。"""
    if year <= base_year:
        return 1.0
    f = 1.0
    for y in range(base_year + 1, year + 1):
        for until, rate in segments:
            if until is None or y <= until:
                f *= rate
                break
        else:
            # 无开放末端段却超出所有分段 → 视为停止增长（防御性；当前配置不会走到）
            pass
    return f


def property_factor(year: int) -> float:
    """经营性房产分段复利系数（1974 基桩=1）。"""
    return compound_factor(year, _PROPERTY_SEGMENTS)


def rent_factor(year: int) -> float:
    """惠民租房分段复利系数（1974 基桩=1）。"""
    return compound_factor(year, _RENT_SEGMENTS)
