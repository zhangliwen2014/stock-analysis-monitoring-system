"""A 股主源：akshare。

抓取函数可注入（`fetch`），因此解析与规范化路径能在无网络环境下被测试。
这是本项目所有适配器的统一形态：IO 边界注入，纯逻辑直测。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import polars as pl

from sams.core.errors import DatasourceError, EmptyResponse
from sams.datasource.base import (
    classify_error,
    code_in_market,
    normalize_bars,
)

COLUMN_MAP = {
    "date": "日期",
    "open": "开盘",
    "high": "最高",
    "low": "最低",
    "close": "收盘",
    "volume": "成交量",
    "amount": "成交额",
}


def akshare_fetch(symbol: str, start: date, end: date) -> pl.DataFrame:
    """真实网络调用。列名为中文，与 akshare.stock_zh_a_hist 一致。"""
    import akshare as ak

    frame = ak.stock_zh_a_hist(
        symbol=symbol,
        period="daily",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        adjust="",
    )
    if frame is None or len(frame) == 0:
        raise EmptyResponse("akshare", f"{symbol} 区间无数据")
    return pl.from_pandas(frame)


class AkshareAdapter:
    name = "akshare"
    markets = frozenset({"A"})

    def __init__(self, fetch: Callable[[str, date, date], pl.DataFrame] = akshare_fetch) -> None:
        self._fetch = fetch

    def daily_bars(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        symbol = code_in_market(market, code)
        try:
            raw = _as_polars(self._fetch(symbol, start, end))
            if raw.is_empty():
                raise EmptyResponse(self.name, f"{market} {code} 区间无数据")
            bars = normalize_bars(
                raw, market=market, code=code, source=self.name, column_map=COLUMN_MAP
            )
        except DatasourceError:
            raise  # 已经是本域异常（EmptyResponse / FieldDrift），别二次归类
        except Exception as exc:  # noqa: BLE001 — 边界处统一归类，见 classify_error
            raise classify_error(self.name, exc) from exc
        return bars



def _as_polars(frame) -> pl.DataFrame:
    if isinstance(frame, pl.DataFrame):
        return frame
    return pl.from_pandas(frame)
