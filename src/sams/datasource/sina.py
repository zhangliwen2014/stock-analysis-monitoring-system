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
from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift, RateLimited
from sams.datasource.base import a_share_symbol, classify_error, normalize_bars
from sams.datasource.fx import normalize_fx
from sams.datasource.sources import source

KLINE_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
#: 接口单次最多返回的根数（超过就只给最近这么多，不报错）
MAX_BARS = 1023
#: 外汇日 K 走 JSONP：变量名里带着方向，解析时拿它核对请求的那一对
FX_URL = (
    "https://vip.stock.finance.sina.com.cn/forex/api/jsonp.php"
    "/var%20_{symbol}=/NewForexService.getDayKLine"
)

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


def parse_sina_minute(payload: object, *, code: str, start: date, end: date) -> pl.DataFrame:
    """纯解析：新浪分钟 JSON → `MINUTE_BARS_COLUMNS`，按 `[start, end]` 裁剪。

    `day` 在这一档是 `"2026-10-28 11:05:00"`（带秒），比日线多一段时间；拆成
    `date` + `clock` 是因为 `normalize_bars` 会按 `date` 去重——一分钟一根，
    整场交易会被它压成一根 K，而那看起来像「这个源只给了一天」。
    """
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, list):
        raise DatasourceError(
            "sina", f"分钟响应不是数组: {type(payload).__name__}；原文 {str(payload)[:120]}"
        )
    if not payload:
        raise EmptyResponse("sina", f"{code} 分钟区间 0 根（响应为空数组）")
    rows = [item for item in payload if isinstance(item, dict)]
    if len(rows) != len(payload):
        raise DatasourceError("sina", "分钟响应里混入非对象元素，字段形态已变")

    moments = [str(item.get("day") or "") for item in rows]
    bad = [value for value in moments if len(value) < 16]
    if bad:
        raise FieldDrift("sina", f"分钟行的 `day` 形态不对: {bad[0]!r}")

    frame = pl.DataFrame(
        {
            "market": ["A"] * len(rows),
            "code": [code] * len(rows),
            "date": [value[:10] for value in moments],
            "clock": [value[11:16] for value in moments],
            "open": [item.get("open") for item in rows],
            "high": [item.get("high") for item in rows],
            "low": [item.get("low") for item in rows],
            "close": [item.get("close") for item in rows],
            "volume": [item.get("volume") for item in rows],
            "amount": [item.get("amount") for item in rows],
            "source": ["sina"] * len(rows),
        }
    )
    numeric = ("open", "high", "low", "close", "volume", "amount")
    frame = frame.with_columns(
        pl.col("date").str.to_date("%Y-%m-%d"),
        *[pl.col(name).cast(pl.Float64) for name in numeric],
    )
    return frame.sort(["date", "clock"]).filter(pl.col("date").is_between(start, end))


def http_get(url: str, params: dict[str, str], *, timeout: int = 15) -> object:
    import requests

    response = requests.get(
        url, params=params, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}
    )
    if response.status_code == 429:
        raise RateLimited("sina", "HTTP 429 被限流")
    response.raise_for_status()
    return json.loads(response.text)


def http_get_text(url: str, params: dict[str, str], *, timeout: int = 15) -> str:
    """外汇接口回的是 JSONP 文本：交给 `http_get` 会把它当成坏 JSON。"""
    import requests

    response = requests.get(
        url,
        params=params,
        timeout=timeout,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        },
    )
    if response.status_code == 429:
        raise RateLimited("sina", "HTTP 429 被限流")
    response.raise_for_status()
    return response.text


def fx_symbol(base: str, quote: str) -> str:
    """方向由拼接顺序决定：`fx_shkdcny` 与 `fx_scnyhkd` 是两条不同的序列。"""
    return f"fx_s{base.lower()}{quote.lower()}"


def parse_sina_fx(payload: object, *, base: str, quote: str) -> pl.DataFrame:
    """JSONP → 标准汇率帧，取每日**收盘**（一行五格里的第 5 格）。

    变量名必须是请求的那一对：反方向的序列同样是一串「看起来正常」的汇率，
    收下它，每一笔跨币种盈亏都会差近 40%。收盘的位置用相邻两行钉住——
    今天的开盘等于昨天的收盘，而第 4 格不满足这条。
    """
    if not isinstance(payload, str):
        raise DatasourceError("sina", f"汇率响应不是 JSONP 文本: {type(payload).__name__}")
    symbol = fx_symbol(base, quote)
    marker = f'var _{symbol}=("'
    start = payload.find(marker)
    if start < 0:
        raise FieldDrift("sina", f"汇率响应里找不到 var _{symbol}=(\"，形态已变或方向不对")
    start += len(marker)
    end = payload.rfind('")')
    body = payload[start:end] if end >= start else ""
    rows: list[dict[str, str]] = []
    for line in body.split("|"):
        text = line.strip()
        if not text:
            continue
        parts = text.split(",")
        if len(parts) < 5:
            # 少一格是改版：静默跳过的那一天不会有人发现，直到锚上它
            raise FieldDrift("sina", f"汇率行 {text[:40]!r} 不足五格（日期、开、低、高、收）")
        rows.append({"date": parts[0], "rate": parts[4]})
    if not rows:
        raise EmptyResponse("sina", f"{base}→{quote} 的汇率响应里没有一行")
    return normalize_fx(pl.DataFrame(rows), base=base, quote=quote, source="sina")


class SinaAdapter:
    name = "sina"
    markets = frozenset({"A"})
    #: 分钟 K 只有 A 股：新浪这一路本来就没有港美股
    minute_bars_markets = frozenset({"A"})

    def __init__(
        self,
        *,
        get: Callable[..., object] = http_get,
        fx_get: Callable[..., object] = http_get_text,
        timeout: int = 15,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._get = get
        self._fx_get = fx_get
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

    def index_daily(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        """指数日线：与 `daily_bars` 同一个端点，symbol 用源形态指数码原样（D148）。

        不过 `a_share_symbol`：`sh000001` 再折一次会归成 `000001` 拼成 `sz000001`，
        问成平安银行；新浪只有 A 股，别的市场按 `EmptyResponse` 答（是答复不是故障）。
        """
        if market != "A":
            raise EmptyResponse("sina", f"新浪指数日线只有 A 股，收到 {market}")
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
            "symbol": code,
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
        # normalize_bars 把 code 归一个股形态，而指数表的键要源形态（sh000001 ≠ 平安银行，D145）
        return frame.with_columns(pl.lit(code).alias("code"))

    def fx_rate(self, base: str, quote: str, start: date, end: date) -> pl.DataFrame:
        """人民币汇率日线：`X→CNY` 与 `CNY→X` 各自有代码，方向不靠取倒数。

        源给全史（2014 起），窗口裁剪在 `registry._call_fx`——那里对每条链只做一次，
        适配器再裁一遍会让「这条源到过哪一天」多出第二个说法。
        """
        if end < start:
            raise ValueError(f"区间反向: {start} > {end}")
        base, quote = base.upper(), quote.upper()
        symbol = fx_symbol(base, quote)
        try:
            payload = self._fx_get(
                FX_URL.format(symbol=symbol), {"symbol": symbol}, timeout=self._timeout
            )
        except Exception as exc:  # noqa: BLE001 — 只包网络这一跳
            raise classify_error(self.name, exc) from exc
        return parse_sina_fx(payload, base=base, quote=quote)

    def minute_bars(
        self, market: str, code: str, start: date, end: date, *, scale: int, count: int
    ) -> pl.DataFrame:
        if market not in self.minute_bars_markets:
            raise EmptyResponse(
                "sina", f"新浪分钟K只供 {sorted(self.minute_bars_markets)}，收到 {market}"
            )
        if end < start:
            raise ValueError(f"区间反向: {start} > {end}")
        params = {
            "symbol": a_share_symbol(code),
            # `scale` 就是分钟数；`datalen` 真被 honor，所以这里不需要任何「每档上限」表
            "scale": str(scale),
            "ma": "no",
            "datalen": str(count),
        }
        try:
            payload = self._get(KLINE_URL, params, timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 — 只包网络这一跳
            raise classify_error(self.name, exc) from exc
        frame = parse_sina_minute(payload, code=code, start=start, end=end)
        if frame.is_empty():
            # 参数里 `scale` 填错时接口只回 `[]`：那是「这个源没答」，不是「这只票没数据」
            raise EmptyResponse(
                "sina", f"{code} 的 {scale} 分钟K 在 {start}~{end} 内 0 根（已请求 {count} 根）"
            )
        return frame


source(
    "sina",
    options=("timeout",),
    note="新浪 A 股日线 + 指数日线 + 分钟K + 人民币汇率日线，日线只覆盖最近约 1023 根",
)(SinaAdapter)
