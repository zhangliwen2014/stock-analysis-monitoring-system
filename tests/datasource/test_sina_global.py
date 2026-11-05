"""新浪港美股日线（akshare 的 `stock_hk_daily` / `stock_us_daily` / `index_us_stock_sina`）。

夹具是 2026-11-05 实测读数，形状与数值都照抄，接口的三条硬事实写在模块文档里：
HK 带成交额、US 不带、US 指数的成交额恒为 0。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from sams.core.errors import EmptyResponse, FieldDrift, TransientError
from sams.datasource.base import BAR_COLUMNS
from sams.datasource.sina_global import SinaGlobalAdapter, sina_us_ticker

D0 = date(2026, 11, 4)
D1 = date(2026, 11, 5)

#: 00700 实测：5508 行 / 7 列，成交量与成交额都是「股」「元」原值，不换算
HK_RAW = pl.DataFrame(
    {
        "date": [D0, D1],
        "open": [397.8, 413.8],
        "high": [402.2, 431.2],
        "low": [391.6, 410.2],
        "close": [401.60001, 430.39999],
        "volume": [18500105.0, 56903938.0],
        "amount": [7372563093.0, 24168809665.0],
    }
)
#: AAPL 实测：10068 行 / 6 列，接口**没有**成交额列
US_RAW = pl.DataFrame(
    {
        "date": [D0, D1],
        "open": [325.58, 341.565],
        "high": [334.5, 354.5],
        "low": [325.33, 340.99],
        "close": [331.9, 349.51],
        "volume": [63160025.0, 89637676.0],
    }
)
#: .IXIC 实测：5747 行 / 7 列，但 amount 整列为 0——指数没有成交额，0 不是事实
IDX_RAW = pl.DataFrame(
    {
        "date": [D0, D1],
        "open": [25246.6348, 25652.7812],
        "high": [25474.4629, 26048.8301],
        "low": [25246.6348, 25602.1367],
        "close": [25372.0098, 25736.5137],
        "volume": [6369794286.0, 7438536059.0],
        "amount": [0.0, 0.0],
    }
)


class FakeFetch:
    def __init__(
        self, frames: dict[str, pl.DataFrame] | None = None, error: Exception | None = None
    ):
        self.frames = frames or {}
        self.error = error
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> pl.DataFrame:
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        return self.frames.get(args[0], pl.DataFrame())


def adapter_with(
    *, daily: FakeFetch | None = None, index: FakeFetch | None = None
) -> tuple[SinaGlobalAdapter, FakeFetch, FakeFetch]:
    daily = daily or FakeFetch()
    index = index or FakeFetch()
    adapter = SinaGlobalAdapter(daily_fetch=daily, index_fetch=index)
    return adapter, daily, index


# --- 日线：HK 与 US 的列集不是同一张 ------------------------------------------------


def test_hk_daily_keeps_amount_and_asks_for_the_five_digit_symbol():
    adapter, daily, _ = adapter_with(daily=FakeFetch({"00700": HK_RAW}))
    out = adapter.daily_bars("HK", "700", D0, D1)

    assert daily.calls == [("00700", "HK")]
    assert out.columns == BAR_COLUMNS
    assert out["code"].unique().to_list() == ["00700"]
    row = out.filter(pl.col("date") == D1).row(0, named=True)
    assert row["close"] == pytest.approx(430.39999)
    # 港股的量与额本来就是股/元，不乘 100 也不除 1e8
    assert row["volume"] == pytest.approx(56903938.0)
    assert row["amount"] == pytest.approx(24168809665.0)


def test_us_daily_amount_is_null_when_the_interface_does_not_give_one():
    """缺列填 NULL 是标准列的语义；填 0 会被下游读成「零成交」。"""
    adapter, daily, _ = adapter_with(daily=FakeFetch({"AAPL": US_RAW}))
    out = adapter.daily_bars("US", "AAPL", D0, D1)

    assert daily.calls == [("AAPL", "US")]
    assert out["amount"].to_list() == [None, None]
    assert out["close"].to_list() == [pytest.approx(331.9), pytest.approx(349.51)]


@pytest.mark.parametrize(("raw_code", "requested"), [("AAPL.OQ", "AAPL"), ("BRK.B", "BRK.B")])
def test_us_ticker_drops_the_exchange_suffix_but_not_the_class_share(raw_code, requested):
    """`stock_us_daily("AAPL.OQ")` 实测抛 IndexError，而 `BRK.B` 是合法类股。"""
    assert sina_us_ticker(raw_code) == requested


def test_us_ticker_strips_a_suffix_after_a_class_share():
    """`BRK.A.N` 的尾巴是交易所而不是类股：留下 `.A`，去掉 `.N`。"""
    assert sina_us_ticker("BRK.A.N") == "BRK.A"


def test_daily_window_is_clipped():
    adapter, _, _ = adapter_with(daily=FakeFetch({"00700": HK_RAW}))
    out = adapter.daily_bars("HK", "00700", D1, D1)
    assert out["date"].to_list() == [D1]


def test_daily_window_without_rows_is_an_empty_answer():
    adapter, _, _ = adapter_with(daily=FakeFetch({"00700": HK_RAW}))
    with pytest.raises(EmptyResponse):
        adapter.daily_bars("HK", "00700", date(2020, 1, 2), date(2020, 1, 3))


def test_daily_empty_frame_is_an_empty_answer():
    adapter, daily, _ = adapter_with(daily=FakeFetch({"00700": pl.DataFrame()}))
    with pytest.raises(EmptyResponse):
        adapter.daily_bars("HK", "00700", D0, D1)
    assert daily.calls == [("00700", "HK")]


def test_daily_missing_close_is_field_drift():
    adapter, _, _ = adapter_with(daily=FakeFetch({"AAPL": US_RAW.drop("close")}))
    with pytest.raises(FieldDrift):
        adapter.daily_bars("US", "AAPL", D0, D1)


def test_daily_transport_error_is_classified():
    adapter, _, _ = adapter_with(daily=FakeFetch(error=ConnectionError("reset by peer")))
    with pytest.raises(TransientError):
        adapter.daily_bars("US", "AAPL", D0, D1)


def test_a_market_is_not_supported():
    adapter, daily, _ = adapter_with()
    with pytest.raises(ValueError):
        adapter.daily_bars("A", "600519", D0, D1)
    assert daily.calls == [], "拒绝必须发生在请求之前"


# --- 指数日线：只有 US，且请求键与入库键不同 ----------------------------------------


def test_us_index_maps_to_the_sina_symbol_and_keeps_the_requested_code():
    adapter, _, index = adapter_with(index=FakeFetch({".IXIC": IDX_RAW}))
    out = adapter.index_daily("US", "usIXIC", D0, D1)

    assert index.calls == [(".IXIC",)]
    # index_daily 表的键是本系统的指数码（usIXIC），不是新浪的 .IXIC
    assert out["code"].unique().to_list() == ["usIXIC"]
    assert out["close"].to_list() == [pytest.approx(25372.0098), pytest.approx(25736.5137)]


def test_us_index_amount_is_null_not_zero():
    """指数没有成交额，接口给的 0 是缺省值；存 0 会让「零成交」混进成交额统计。"""
    adapter, _, _ = adapter_with(index=FakeFetch({".INX": IDX_RAW}))
    out = adapter.index_daily("US", "usINX", D0, D1)
    assert out["amount"].to_list() == [None, None]


def test_unknown_us_index_is_an_empty_answer():
    adapter, _, index = adapter_with()
    with pytest.raises(EmptyResponse):
        adapter.index_daily("US", "usRUT", D0, D1)
    assert index.calls == [], "构造不出符号时不该发出请求"


def test_hk_index_is_an_empty_answer():
    """新浪这一路没有港股指数：这是答复不是故障，降级链要继续问下一个源。"""
    adapter, _, index = adapter_with()
    with pytest.raises(EmptyResponse):
        adapter.index_daily("HK", "hkHSI", D0, D1)
    assert index.calls == []


def test_index_transport_error_is_classified():
    adapter, _, _ = adapter_with(index=FakeFetch(error=TimeoutError("read timed out")))
    with pytest.raises(TransientError):
        adapter.index_daily("US", "usIXIC", D0, D1)
