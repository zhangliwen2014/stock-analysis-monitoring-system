"""分钟粒度两条能力的探针、列集、空帧与意愿表。

与 `quote.py` / `actions.py` 同一位置：这类「一种数据一小节」的模块都只放纯函数与常量，
不 import registry，所以 registry、服务、Web 三层都能拿它做同一份判定。

`answers` 为什么和探针分开：探针答的是「这个适配器有没有这个能力」（`CAPABILITIES`
唯一判据的市场无关那一半），而「这个市场它答不答」按 `<方法>_markets` 走。
同一个 `minute_bars` 方法在腾讯上是 A 股能答、港股不能答，塞成一个判据就必然
在某一侧说谎。
"""

from __future__ import annotations

from typing import Any

import polars as pl

#: 分时的一条记录：某一天某一分钟的一个价（腾讯给的就是这个形状）
MINUTE_SERIES_COLUMNS: list[str] = [
    "market",
    "code",
    "date",
    "time",
    "price",
    "volume",
    "amount",
    "prev_close",
    "source",
]
#: 分钟 K 的一根：`clock` 是这一根的**结束时刻**（"HH:MM"）
MINUTE_BARS_COLUMNS: list[str] = [
    "market",
    "code",
    "date",
    "clock",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "source",
]

_MINUTE_SERIES_TYPES = {
    "market": pl.Utf8,
    "code": pl.Utf8,
    "date": pl.Date,
    "time": pl.Utf8,
    "price": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
    "prev_close": pl.Float64,
    "source": pl.Utf8,
}
_MINUTE_BARS_TYPES = {
    "market": pl.Utf8,
    "code": pl.Utf8,
    "date": pl.Date,
    "clock": pl.Utf8,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
    "source": pl.Utf8,
}


def has_minute_series(obj: Any) -> bool:
    return callable(getattr(obj, "minute_series", None))


def has_minute_bars(obj: Any) -> bool:
    return callable(getattr(obj, "minute_bars", None))


def empty_minute_series() -> pl.DataFrame:
    return pl.DataFrame(
        {
            name: pl.Series(name, [], dtype=_MINUTE_SERIES_TYPES[name])
            for name in MINUTE_SERIES_COLUMNS
        }
    )


def empty_minute_bars() -> pl.DataFrame:
    return pl.DataFrame(
        {
            name: pl.Series(name, [], dtype=_MINUTE_BARS_TYPES[name])
            for name in MINUTE_BARS_COLUMNS
        }
    )


def answers(obj: Any, method: str, market: str) -> bool:
    """这个源愿不愿意答**这个市场**的这一档；没有 `<方法>_markets` 就算愿意。

    它同时是三处的唯一判据：适配器自己的拒答、健康页那个「仅…」标注、周期条上的置灰。
    """
    wanted = getattr(obj, f"{method}_markets", None)
    return wanted is None or market in wanted
