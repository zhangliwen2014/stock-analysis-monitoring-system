"""百度股市通按日估值（A / HK）→ `VALUATION_COLUMNS`（#387 / D324）。

A 股的按日估值原来只有东财一家（`akshare` 这个源名背后的上游），11-05 那晚东财在
`result: null` 上崩了 38 次（D327），估值链上再没有第二个源可换。百度股市通是另一个域名，
一行一指标地答（`date,value`），所以三列要问三次。

四条实测（11-06 本机）决定的形状：

* **一次只答一个指标**：`市盈率(TTM)`/`市净率`/`总市值` 各一次请求；
* **总市值的单位是亿元**，而 `valuation_daily.market_cap` 是元（D239）——少乘 1e8，
  市值会从「万亿」变成「千亿」，而屏幕上只是数字小了一截；
* **只有 `近一年`/`近三年` 是逐日序列**：近五年两天一格、近十年五天一格、全部约十五天
  一格。拿降采样序列冒充按日，PR 会读到一条看似连续的历史；
* 这一路**没有股息率**，那一列留 NULL，不拿别的比率凑。

| 指标 | 标准列 | 2024-06-28 实测（600519 / 00700） |
|---|---|---|
| 市盈率(TTM) | pe_ttm | 23.63 / 24.06 |
| 市净率 | pb | 7.69 / 3.72 |
| 总市值（亿元） | market_cap | 18433.32 / 34836.52 |
"""

from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest
from sams.datasource.baidu_valuation import BaiduValuationAdapter
from support import FrozenClock

from sams.core.errors import EmptyResponse, FieldDrift, TransientError
from sams.core.fundamentals import VALUATION_COLUMNS

D1 = date(2024, 6, 27)
D2 = date(2024, 6, 28)
TODAY = FrozenClock(datetime(2024, 6, 28, 16, 0))

PE = pl.DataFrame({"date": [D1, D2], "value": [24.0, 23.63]})
PB = pl.DataFrame({"date": [D1, D2], "value": [7.81, 7.69]})
CAP = pl.DataFrame({"date": [D1, D2], "value": [18723.5, 18433.32]})
FRAMES = {"市盈率(TTM)": PE, "市净率": PB, "总市值": CAP}


def make_fetch(frames=None, calls=None, *, error=None):
    def fetch(market, symbol, indicator, period):
        if calls is not None:
            calls.append((market, symbol, indicator, period))
        if error is not None:
            raise error
        return {} if frames is None else frames.get(indicator, pl.DataFrame())

    return fetch


def adapter(frames=None, calls=None, *, error=None, clock=TODAY):
    return BaiduValuationAdapter(
        fetch=make_fetch(FRAMES if frames is None else frames, calls, error=error),
        clock=clock,
    )


def test_three_indicator_requests_land_in_one_standard_row_per_day():
    out = adapter().valuation("A", "600519", D1, D2)

    assert tuple(out.columns) == VALUATION_COLUMNS
    assert out["date"].to_list() == [D1, D2]
    assert out["pe_ttm"].to_list() == [24.0, 23.63]
    assert out["pb"].to_list() == [7.81, 7.69]
    assert out["source"].to_list() == ["baidu_valuation", "baidu_valuation"]
    assert out["market"].to_list() == ["A", "A"]
    assert out["code"].to_list() == ["600519", "600519"]


def test_market_cap_arrives_in_yi_and_is_stored_in_yuan():
    """亿元 → 元只差 1e8，混用不会报错，只会让市值静默小两个数量级（D239）。"""
    row = adapter().valuation("A", "600519", D2, D2).row(0, named=True)

    assert row["market_cap"] == pytest.approx(1.843332e12)


def test_the_source_has_no_dividend_yield_so_that_column_stays_null():
    row = adapter().valuation("A", "600519", D2, D2).row(0, named=True)

    assert row["dividend_yield"] is None


def test_an_indicator_whose_values_are_all_null_keeps_the_other_two():
    """总市值缺一天不是「这只票没有估值」：PE/PB 照常入库，缺的那一格留 NULL。"""
    frames = dict(FRAMES)
    frames["总市值"] = pl.DataFrame(
        {"date": [D1, D2], "value": [None, None]},
        schema_overrides={"value": pl.Float64},
    )

    out = adapter(frames).valuation("A", "600519", D1, D2)

    assert out["pe_ttm"].to_list() == [24.0, 23.63]
    assert out["market_cap"].to_list() == [None, None]


def test_no_number_on_any_indicator_is_an_empty_answer_not_a_zero_row_confirm():
    frames = {name: pl.DataFrame() for name in FRAMES}

    with pytest.raises(EmptyResponse, match="baidu_valuation"):
        adapter(frames).valuation("A", "600519", D1, D2)


def test_only_the_daily_periods_are_asked():
    """近五年起上游降采样（2/5/15 天一格），这一路只问逐日的两档。"""
    calls = []
    adapter(calls=calls).valuation("A", "600519", date(2024, 1, 1), D2)

    assert {period for _, _, _, period in calls} == {"近一年"}


def test_a_window_older_than_one_year_asks_for_three_years_and_never_more():
    calls = []
    adapter(calls=calls).valuation("A", "600519", date(2022, 6, 1), D2)

    assert {period for _, _, _, period in calls} == {"近三年"}


def test_a_window_older_than_three_years_stays_on_the_dailiest_period():
    """源没有更早的逐日序列：问 `近三年` 再按窗口裁，交回 0 行比交回半月一格诚实。"""
    calls = []
    out = adapter(calls=calls).valuation("A", "600519", date(2015, 1, 1), date(2016, 12, 31))

    assert {period for _, _, _, period in calls} == {"近三年"}
    assert out.is_empty()


def test_the_window_cuts_the_series():
    out = adapter().valuation("A", "600519", D2, D2)

    assert out["date"].to_list() == [D2]


def test_each_market_asks_its_own_symbol_form():
    """A 股要 6 位裸代码，港股要 5 位带前导零（`700` 传下去是另一只票）。"""
    calls_a, calls_hk = [], []
    adapter(calls=calls_a).valuation("A", "600519", D1, D2)
    adapter(calls=calls_hk).valuation("HK", "700", D1, D2)

    assert {symbol for _, symbol, _, _ in calls_a} == {"600519"}
    assert {symbol for _, symbol, _, _ in calls_hk} == {"00700"}
    assert {market for market, _, _, _ in calls_hk} == {"HK"}


def test_the_us_market_is_out_of_scope():
    with pytest.raises(ValueError, match="不支持市场"):
        adapter().valuation("US", "AAPL", D1, D2)


def test_a_connection_error_is_transient():
    with pytest.raises(TransientError):
        adapter(error=ConnectionError("boom")).valuation("A", "600519", D1, D2)


def test_an_indicator_frame_without_value_is_drift():
    frames = {"市盈率(TTM)": pl.DataFrame({"date": [D1]}), "市净率": PB, "总市值": CAP}

    with pytest.raises(FieldDrift, match="baidu_valuation"):
        adapter(frames).valuation("A", "600519", D1, D2)
