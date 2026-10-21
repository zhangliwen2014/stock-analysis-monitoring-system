"""把后复权口径的数搬回现价口径（spec §5.5 / D73）。

库里的指标都是**在后复权序列上**算的（跨除权日才可比），而用户看的是现价、交易的是现价，
阈值也是按现价填的。所以每一路指标出门前都要乘一个 `现价 ÷ 当日后复权收盘`。

这个文件存在的理由是「同一件外套不能有两件」：`services/valuation.py` 用它合成结论，
`services/rules/` 用它判止损。两边各写一遍的后果是除权日只有一侧折回，于是那条止损在
分红当天报「已跌破」——§10.2 把它列为强制验收用例，就是这个原因。

因子必须来自**它自己那条序列**（周线用周线的当日收盘与当日后复权收盘）：后复权的绝对
水平跟着读取窗口的锚走，两条序列的锚可能不在同一天，而比值在同一序列内与锚无关。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import TYPE_CHECKING, Any

import polars as pl

if TYPE_CHECKING:
    from sams.analysis.adjust import DayFactor


def fold(price: float | None, hfq_last: float | None) -> float | None:
    """后复权 → 现价的换算系数。两个数齐了才有它，否则 None（合成侧自己会拒）。"""
    if price is None or hfq_last is None or hfq_last <= 0 or price <= 0:
        return None
    return price / hfq_last


def fold_to_price_basis(value: float | None, factor: float | None) -> float | None:
    if value is None or factor is None:
        return None
    return value * factor


def last(table: pl.DataFrame, column: str) -> Any:
    if table.height == 0 or column not in table.columns:
        return None
    return table[column][-1]


def before_last(table: pl.DataFrame, column: str) -> Any:
    if table.height < 2 or column not in table.columns:
        return None
    return table[column][-2]


def window_high(table: pl.DataFrame, bars: int) -> float | None:
    """窗口内最高的那根后复权收盘（**还没**折回现价口径：调用方拿自己那条序列的因子折它）。

    用后复权而不是裸价：跨除权日时裸价那个「下跌」是分红，不是有人付过更低的价。
    """
    if table.height == 0 or bars <= 0 or "close_hfq" not in table.columns:
        return None
    return table["close_hfq"].tail(bars).max()


def high_water(table: pl.DataFrame, bars: int) -> float | None:
    """窗口内最高的那根后复权**最高价**：持仓期 peak 用它（spec §9.4 说的是 bars.high）。

    与 `window_high` 一样**还没**折回现价口径；调用方拿自己那条序列的因子折它。
    用后复权而不是裸价：跨除权日时裸价那个「下跌」是分红，不是有人付过更低的价。
    """
    if table.height == 0 or bars <= 0 or "high_hfq" not in table.columns:
        return None
    return table["high_hfq"].tail(bars).max()


def pct_change(table: pl.DataFrame, column: str) -> float | None:
    """最后一根相对上一根的涨跌幅（在后复权口径里算：跨除权日时裸价那个差是分红）。"""
    now, prior = last(table, column), before_last(table, column)
    if now is None or prior is None or prior <= 0:
        return None
    return now / prior - 1.0


def weekly_line(weekly_table: pl.DataFrame) -> float | None:
    """周 MA20 折回现价口径：用它**自己那条序列**的当日因子，不借日线的。"""
    return fold_to_price_basis(
        last(weekly_table, "ma20"),
        fold(last(weekly_table, "close"), last(weekly_table, "close_hfq")),
    )


def steps_after(factors: Sequence[DayFactor], *, after: date) -> tuple[DayFactor, ...]:
    """除权日历里落在某天**之后**的那些档，`after` 当天除外。

    一个数写在 X 日，指的是 X 日那根 K 线的坐标系，而 X 日当天的除权已经反映在那根 K 线里
    （`hfq_frame` 的累乘口径是「除权日 ≤ t」）。留着它再搬一次，等于把同一次派息除了两遍。
    """
    return tuple(step for step in factors if step.ex_date > after)


def shift(value: float, steps: Sequence[DayFactor]) -> float:
    """把一个数搬进今天那侧：除以它之后每一个除权日的因子。

    因子说「今天的价要乘多少才连得上昨天」（D34：当日因子 = 前收 / 除权参考价），
    所以昨天写下的一个价要往**反方向**走。10 派 1、前收 10.00 的那天因子是 1.1111，
    写在它之前的「10 元」于是站在 9.00——券商在除权日调整成本价是同一个动作。
    """
    arrived = value
    for step in steps:
        arrived /= step.factor
    return arrived
