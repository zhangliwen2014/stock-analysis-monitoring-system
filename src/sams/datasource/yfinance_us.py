"""美股 / 港股日线源：yfinance。

只提供日线（M1 范围）。盘中快照在 M3 接入，届时新增 SnapshotAdapter 协议而不是
往本类里塞方法——保持「一个类一个职责」。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

import polars as pl

from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift
from sams.datasource.base import (
    classify_error,
    code_in_market,
    normalize_bars,
)

#: 标准列 → 源列（yfinance 首字母大写）。
#: 这里刻意不映射 amount：`Ticker.history()` 不提供成交额，写个假列名只会让
#: 「港美股成交额不可得」这件事在代码里隐身——规则引擎因此不得对 US/HK 用金额阈值。
COLUMN_MAP = {
    "date": "date",
    "open": "Open",
    "high": "High",
    "low": "Low",
    "close": "Close",
    "volume": "Volume",
}

#: 索引列在 reset_index 之后必须认出来：yfinance 各版本给的名字不一样
DATE_INDEX_NAMES = {"date", "datetime", "index", ""}


def yfinance_fetch(symbol: str, start: date, end: date):
    """走 Ticker.history 而不是 yf.download。

    `download()` 把失败（含 429）吞进内部 ctx.errors 只打印日志，返回空帧，
    调用方就分不清「停牌」与「被限流」；history() 会真的抛出来。
    history 的 end 是开区间，而本系统的区间参数按闭区间语义传进来。
    """
    import yfinance as yf

    return yf.Ticker(symbol).history(
        start=start.strftime("%Y-%m-%d"),
        end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
        auto_adjust=False,
        actions=False,
    )


def flatten_columns(frame) -> None:
    """yfinance 单标的也可能返回 MultiIndex 列，统一压平成首字母大写的单层列名。"""
    if hasattr(frame.columns, "nlevels") and frame.columns.nlevels > 1:
        frame.columns = frame.columns.get_level_values(0)


def normalize_history(frame, *, market: str, code: str) -> pl.DataFrame:
    """录制/抓取到的 pandas 历史 → 标准列集（纯函数，测试入口）。"""
    flatten_columns(frame)
    if frame.empty:
        raise EmptyResponse("yfinance", f"{market} {code} 区间无数据")
    prepared = frame.reset_index()
    prepared.columns = [str(c) for c in prepared.columns]
    date_column = next((c for c in prepared.columns if c.lower() in DATE_INDEX_NAMES), None)
    if date_column is None:
        raise FieldDrift(
            "yfinance", f"{market} {code} 找不到日期列，实际列 {list(prepared.columns)}"
        )
    prepared = prepared.rename(columns={date_column: "date"})
    return normalize_bars(
        pl.from_pandas(prepared),
        market=market,
        code=code,
        source="yfinance",
        column_map=COLUMN_MAP,
    )


class YfinanceAdapter:
    name = "yfinance"
    markets = frozenset({"US", "HK"})

    def __init__(self, fetch: Callable[[str, date, date], object] = yfinance_fetch) -> None:
        self._fetch = fetch

    def daily_bars(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        symbol = code_in_market(market, code)
        try:
            return normalize_history(self._fetch(symbol, start, end), market=market, code=code)
        except DatasourceError:
            raise  # 已经是本域异常，别二次归类
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc
