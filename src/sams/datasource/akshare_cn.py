"""A 股主源：akshare。

抓取函数可注入（`fetch`），因此解析与规范化路径能在无网络环境下被测试。
这是本项目所有适配器的统一形态：IO 边界注入，纯逻辑直测。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import polars as pl

from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift
from sams.datasource.base import (
    LOTS_TO_SHARES,
    classify_error,
    code_in_market,
    normalize_bars,
)
from sams.datasource.sources import source

COLUMN_MAP = {
    "date": "日期",
    "open": "开盘",
    "high": "最高",
    "low": "最低",
    "close": "收盘",
    "volume": "成交量",
    "amount": "成交额",
}

#: akshare 把请求的代码回写在每一行里，拿它确认取到的确实是那只票
ECHO_COLUMN = "股票代码"


def require_echoed_code(frame: pl.DataFrame, *, code: str) -> None:
    """回写的代码与请求不一致 → 整批都不能要。

    换错票不会露出破绽：`000001` 的曲线挂到 `600519` 名下，看起来只是一只跌得很难看的
    茅台，而它已经是库里的事实，事后无从查出。响应里没有这一列时不拒绝——没证据不等于矛盾。
    """
    if ECHO_COLUMN not in frame.columns:
        return
    echoed = {str(value) for value in frame[ECHO_COLUMN].unique().to_list()}
    if echoed != {code}:
        raise FieldDrift("akshare", f"响应回写的代码是 {sorted(echoed)}，请求的是 {code}")


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
            require_echoed_code(raw, code=symbol)
            bars = normalize_bars(
                raw,
                market=market,
                code=code,
                source=self.name,
                column_map=COLUMN_MAP,
                volume_scale=LOTS_TO_SHARES,
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


# 注册进 datasource.sources：加源只改自己这一行，不动 registry / bootstrap / CLI
source("akshare", note="akshare A 股主源（封装东财/新浪）")(AkshareAdapter)
