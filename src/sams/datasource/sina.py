"""新浪行情 A 股日线备源。

与 akshare / eastmoney / 腾讯 同一条降级链上的第四级，价值在于它属于**另一套基础设施**
（sina 的机房与限流策略和东方财富、腾讯互不相干）：主源被限流时，多一个独立故障域比
多一个同源备份有用得多。

它有三个必须写进代码的硬限制：
  1. **只能取最近 N 根**，接口不接受起止日期。所以区间超出覆盖范围时明确报错换源，
     而不是「给多少算多少」——一根被静默截断的序列看起来和完整序列一模一样。
  2. 返回的是**最近的**数据，请求区间只是过滤条件：不裁就会把区间外的日子一起入库，
     而 sync 的幂等承诺会变成「多出来的日子从哪来的」。
  3. 数据的右端永远是今天，不是请求的 `end`：要根数必须按 `start → 今天` 折算。按区间
     长度折算会少要，回来再一裁就是 0 根——那看起来像「这个源没有这段数据」，其实是自己没要到。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date

import polars as pl

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import DatasourceError, EmptyResponse, RateLimited
from sams.datasource.base import a_share_symbol, classify_error, normalize_bars
from sams.datasource.sources import source

KLINE_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
#: 接口单次最多返回的根数（超过就只给最近这么多，不报错）
MAX_BARS = 1023

FIELD_ORDER = ("day", "open", "high", "low", "close", "volume")
#: 标准列名 → 源列名：新浪的日期列叫 day
COLUMN_MAP = {
    "date": "day",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
}


def parse_sina_daily(payload: object, *, code: str, start: date, end: date) -> pl.DataFrame:
    """纯解析：新浪 JSON 数组 → 标准列集，并按 [start, end] 裁剪。"""
    if isinstance(payload, str):
        # 这个接口给的是裸 JSON 文本，个别错误场景会夹带 JS 赋值语句
        payload = json.loads(payload)
    if not isinstance(payload, list):
        raise DatasourceError(
            "sina", f"响应不是数组: {type(payload).__name__}；原文 {str(payload)[:120]}"
        )
    if not payload:
        raise EmptyResponse("sina", f"{code} 区间无数据")

    rows = [r for r in payload if isinstance(r, dict)]
    if len(rows) != len(payload):
        raise DatasourceError("sina", "响应数组里混入非对象元素，字段形态已变")
    frame = pl.DataFrame(rows)
    missing = [c for c in ("day", "close") if c not in frame.columns]
    if missing:
        raise DatasourceError("sina", f"缺少必需列 {missing}，实际列 {list(frame.columns)}")

    out = normalize_bars(
        frame,
        market="A",
        code=code,
        source="sina",
        column_map=COLUMN_MAP,
        # 新浪的成交量已经是「股」（茅台一天 2.4e6，而腾讯同日给 2.4e4 手），不换算
        volume_scale=1.0,
    )
    return out.filter(pl.col("date").is_between(start, end))


def http_get(url: str, params: dict[str, str], *, timeout: int = 15) -> object:
    import requests

    response = requests.get(
        url, params=params, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}
    )
    if response.status_code == 429:
        raise RateLimited("sina", "HTTP 429 被限流")
    response.raise_for_status()
    return json.loads(response.text)


class SinaAdapter:
    name = "sina"
    markets = frozenset({"A"})

    def __init__(
        self,
        *,
        get: Callable[..., object] = http_get,
        timeout: int = 15,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._get = get
        self._timeout = timeout
        self._clock = clock

    def daily_bars(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        if end < start:
            raise ValueError(f"区间反向: {start} > {end}")
        today = self._clock.today()
        # 根数必须按 start→**今天** 算，不是 start→end：接口只给「最近 N 根」，
        # 数据右端永远是今天。按区间长度算会少要——然后过滤出 0 根，
        # 一次补数看起来成功，实际什么都没写进去。
        needed = int((today - start).days * 5 / 7) + 5
        if needed > MAX_BARS:
            raise DatasourceError(
                "sina",
                f"请求区间约需 {needed} 根日线，超过本源单次上限 {MAX_BARS} 根"
                f"（约 {MAX_BARS * 7 // 5} 个日历天）；更早的历史请走其它源",
            )
        params = {
            "symbol": a_share_symbol(code),
            "scale": "240",
            "ma": "no",
            "datalen": str(min(needed, MAX_BARS)),
        }
        try:
            payload = self._get(KLINE_URL, params, timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 — 只包网络这一跳，解析异常不该被归成「瞬时故障」
            raise classify_error(self.name, exc) from exc
        frame = parse_sina_daily(payload, code=code, start=start, end=end)
        if frame.is_empty():
            # 请求够了、区间内一根没有：区间太老（本源只覆盖最近约 500 个交易日）
            # 或那段时间确实停市。报「无数据」让路由继续问下一个源，而不是交回空帧
            raise EmptyResponse(
                "sina", f"{code} 在 {start}~{end} 内 0 根（已请求 {needed} 根到今天）"
            )
        return frame


source("sina", options=("timeout",), note="新浪 A 股日线，只有最近约 1023 根")(SinaAdapter)
