"""A 股备源：东方财富公开 kline 接口。

存在的意义是「主源被限流或改版时系统不瞎」（spec §7.2）。
解析函数是纯函数，测试喂录制 payload 即可覆盖字段漂移与空数据分支。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date

import polars as pl

from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift, RateLimited
from sams.datasource.base import (
    classify_error,
    code_in_market,
    normalize_bars,
)

KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
#: 接口按 fields2 的顺序逗号分隔返回，顺序即语义，改动会静默错位
FIELD_ORDER = ("date", "open", "close", "high", "low", "volume", "amount")

COLUMN_MAP = {
    "date": "date",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "amount": "amount",
}


def secid(market: str, code: str) -> str:
    """东财的市场前缀编号：沪 1、深 0、港 116、美 105/106/107。"""
    symbol = code_in_market(market, code)
    if market == "A":
        if not symbol.isdigit() or len(symbol) != 6:
            raise ValueError(f"A 股代码应为 6 位数字，收到 {code!r}")
        return f"{'1' if symbol[0] in '5669' else '0'}.{symbol}"
    if market == "HK":
        return f"116.{symbol.removesuffix('.HK')}"
    if market == "US":
        return f"105.{symbol}"
    raise ValueError(f"未知市场 {market}")


def parse_eastmoney_daily(payload: object, *, market: str, code: str) -> pl.DataFrame:
    """纯解析：接口 JSON → 标准列集 DataFrame。"""
    if not isinstance(payload, dict):
        raise FieldDrift("eastmoney", f"响应不是对象: {type(payload).__name__}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise FieldDrift("eastmoney", "响应缺少 data 节点（接口可能改版）")
    klines = data.get("klines")
    if not klines:
        raise EmptyResponse("eastmoney", f"{market} {code} 区间无数据")

    rows = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < len(FIELD_ORDER):
            raise FieldDrift("eastmoney", f"kline 字段不足: {line!r}")
        rows.append(dict(zip(FIELD_ORDER, parts[: len(FIELD_ORDER)], strict=True)))

    frame = pl.DataFrame(rows).with_columns(
        pl.col("open").cast(pl.Float64, strict=False),
        pl.col("close").cast(pl.Float64, strict=False),
        pl.col("high").cast(pl.Float64, strict=False),
        pl.col("low").cast(pl.Float64, strict=False),
        pl.col("volume").cast(pl.Float64, strict=False),
        pl.col("amount").cast(pl.Float64, strict=False),
    )
    return normalize_bars(
        frame, market=market, code=code, source="eastmoney", column_map=COLUMN_MAP
    )


def http_get(url: str, params: dict[str, str]) -> dict:
    import requests

    response = requests.get(url, params=params, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    if response.status_code == 429:
        raise RateLimited("eastmoney", "HTTP 429 被限流")
    response.raise_for_status()
    return json.loads(response.text)


class EastmoneyAdapter:
    name = "eastmoney"
    markets = frozenset({"A"})

    def __init__(self, get: Callable[[str, dict[str, str]], dict] = http_get) -> None:
        self._get = get

    def daily_bars(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        params = {
            "secid": secid(market, code),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
            "klt": "101",
            "fqt": "0",
            "beg": start.strftime("%Y%m%d"),
            "end": end.strftime("%Y%m%d"),
        }
        try:
            payload = self._get(KLINE_URL, params)
            return parse_eastmoney_daily(payload, market=market, code=code)
        except DatasourceError:
            raise  # 已经是本域异常，别二次归类
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc

