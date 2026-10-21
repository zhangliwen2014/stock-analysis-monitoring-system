"""折回现价口径：`现价 ÷ 当日后复权收盘` 这个比值，以及用它搬的那几路。

算式与 `valuation.py` 里原来那七个私有函数逐字一致——这一层的验收标准是「行为没变」，
所以断言写的都是「同一份输入得到同一个数」，不是「看起来合理」。
"""

from __future__ import annotations

import polars as pl
import pytest

from sams.services._fold import (
    before_last,
    fold,
    fold_to_price_basis,
    high_water,
    last,
    pct_change,
    weekly_line,
    window_high,
)


def test_fold_is_price_over_hfq_close() -> None:
    assert fold(11.0, 100.0) == 0.11


def test_fold_gives_up_when_either_side_is_missing() -> None:
    """None、0、负数都返回 None：合成侧自己会拒，这里不能伪造一个 0 因子。"""
    assert fold(None, 100.0) is None
    assert fold(11.0, None) is None
    assert fold(11.0, 0.0) is None
    assert fold(0.0, 100.0) is None
    assert fold(-1.0, 100.0) is None
    assert fold(11.0, -100.0) is None


def test_folding_a_value_moves_it_by_the_factor() -> None:
    assert fold_to_price_basis(9.0, 0.5) == 4.5
    assert fold_to_price_basis(None, 0.5) is None
    assert fold_to_price_basis(9.0, None) is None


def test_last_is_the_tail_of_a_column_or_none() -> None:
    table = pl.DataFrame({"close": [1.0, 2.0, 3.0]})
    assert last(table, "close") == 3.0
    assert last(table, "nope") is None
    assert last(pl.DataFrame({"close": []}), "close") is None


def test_before_last_is_the_one_before_the_tail_or_none() -> None:
    """上一根取 `[-2]`：两行表上 `[-2]` 与 `[0]` 恰好同值，三行才钉得住下标。"""
    table = pl.DataFrame({"close": [1.0, 2.0, 3.0]})
    assert before_last(table, "close") == 2.0
    assert before_last(table, "nope") is None
    assert before_last(pl.DataFrame({"close": [1.0]}), "close") is None
    assert before_last(pl.DataFrame({"close": []}), "close") is None


def test_window_high_walks_the_adjusted_column() -> None:
    """用 `close_hfq` 而不是裸价：跨除权日时裸价那个「下跌」是分红，不是有人付过更低的价。"""
    table = pl.DataFrame({"close_hfq": [1.0, 5.0, 2.0, 4.0]})
    assert window_high(table, 3) == 5.0
    assert window_high(table, 0) is None
    assert window_high(pl.DataFrame({"close": [1.0]}), 3) is None
    assert window_high(pl.DataFrame({"close_hfq": []}), 3) is None


def test_high_water_walks_the_adjusted_high_column() -> None:
    table = pl.DataFrame({"high_hfq": [1.0, 5.0, 2.0, 4.0]})
    assert high_water(table, 3) == 5.0
    assert high_water(table, 0) is None
    assert high_water(pl.DataFrame({"close_hfq": [1.0]}), 3) is None


def test_pct_change_is_the_ratio_minus_one_on_the_adjusted_series() -> None:
    table = pl.DataFrame({"close_hfq": [10.0, 11.0]})
    # 11/10 - 1 的浮点结果是 0.10000000000000009，算式逐字保留，断言按近似写
    assert pct_change(table, "close_hfq") == pytest.approx(0.1)
    assert pct_change(pl.DataFrame({"close_hfq": [10.0]}), "close_hfq") is None
    assert pct_change(table, "nope") is None
    assert pct_change(pl.DataFrame({"close_hfq": [0.0, 11.0]}), "close_hfq") is None
    assert pct_change(pl.DataFrame({"close_hfq": [-10.0, 11.0]}), "close_hfq") is None


def test_weekly_line_folds_with_its_own_series() -> None:
    """周线用它自己的当日因子：两条序列的后复权锚可能不在同一天（模块开头那条理由）。"""
    weekly = pl.DataFrame({"ma20": [120.0], "close": [110.0], "close_hfq": [100.0]})
    assert weekly_line(weekly) == 132.0  # 120 × (110 / 100)
    assert weekly_line(pl.DataFrame({"ma20": [120.0], "close": [110.0]})) is None
    assert weekly_line(pl.DataFrame()) is None
