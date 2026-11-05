"""百度股市通按日估值（A / HK）：一次答一个指标，三列问三次（#387 / D324）。

东财那一族（`akshare` 这个源名背后的上游）11-05 晚在 `result: null` 上崩了 38 次（D327），
而 A/HK 的按日估值当时都只有它一家。百度股市通是另一个域名，数字对得上：

| 指标 | 标准列 | 单位 |
|---|---|---|
| 市盈率(TTM) | `pe_ttm` | 倍 |
| 市净率 | `pb` | 倍 |
| 总市值 | `market_cap` | **亿元**（入库前乘 1e8，D239 要求元） |

三条实测（11-06 本机）决定了这里的写法：

* 接口一次只答一个指标（`date,value` 两列），所以一只票三次请求；
* 只有 `近一年`/`近三年` 是逐日序列：`近五年` 两天一格、`近十年` 五天一格、`全部`
  约十五天一格。降采样序列冒充按日，PR 会读到一条看似连续的历史，所以更早的窗口
  只问 `近三年` 再按窗口裁，交回 0 行；
* 这一路**没有股息率**，也不给股息率的替代品；`dividend_yield` 留 NULL。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date

import polars as pl

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_code
from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift
from sams.datasource.base import classify_error, to_date_expr
from sams.datasource.fundamentals import normalize_valuation
from sams.datasource.sources import source

#: 标准列 → 上游的指标名。顺序就是请求顺序；三次请求各错各的，不共享一份结果（D322）。
INDICATORS: dict[str, str] = {
    "pe_ttm": "市盈率(TTM)",
    "pb": "市净率",
    "market_cap": "总市值",
}

#: 上游总市值的单位是亿元；`valuation_daily.market_cap` 是元（D239）。少乘这一步不会报错。
YI = 1e8

#: 实测逐日的两档（天数上限）。更宽的档位会被上游降采样，不进这张表。
DAILY_PERIODS: tuple[tuple[str, int], ...] = (("近一年", 366), ("近三年", 3 * 366))


def daily_period(today: date, start: date) -> str:
    """窗口起点落在哪个逐日档里。更早的窗口仍问 `近三年`：这个源没有更细的逐日序列。"""
    age = (today - start).days
    for period, limit in DAILY_PERIODS:
        if age <= limit:
            return period
    return DAILY_PERIODS[-1][0]


def baidu_valuation_indicator(
    market: str, symbol: str, indicator: str, period: str
) -> pl.DataFrame:
    """真实网络调用：某只票的一个估值指标（A 走 gushitong，港股走 finance 域名）。"""
    import akshare as ak

    if market == "A":
        frame = ak.stock_zh_valuation_baidu(symbol=symbol, indicator=indicator, period=period)
    elif market == "HK":
        frame = ak.stock_hk_valuation_baidu(symbol=symbol, indicator=indicator, period=period)
    else:
        raise ValueError(f"百度股市通不服务 {market} 的估值")
    if frame is None or len(frame) == 0:
        return pl.DataFrame()
    return pl.from_pandas(frame)


def parse_baidu_valuation(
    frames: Mapping[str, pl.DataFrame], *, market: str, code: str, start: date, end: date
) -> pl.DataFrame:
    """把几个指标的 `date,value` 按日期拼成 `VALUATION_COLUMNS`，再裁窗口。

    某个指标整列空是「这一路没这个数」，PE/PB 照常入库；三个指标一个数都没有才是
    「源答不上来」——那要抛 `EmptyResponse`，不能写成 0 行的「确认没有」（D43）。
    """
    pieces: list[pl.DataFrame] = []
    for column, raw in frames.items():
        if raw is None or raw.is_empty():
            continue
        absent = [name for name in ("date", "value") if name not in raw.columns]
        if absent:
            raise FieldDrift(
                "baidu_valuation",
                f"{market} {code} 的「{column}」缺少列 {absent}，实际列 {list(raw.columns)}",
            )
        pieces.append(
            raw.select(
                to_date_expr(pl.col("date")).alias("date"),
                pl.col("value").cast(pl.Float64, strict=False).fill_nan(None).alias(column),
            )
            .drop_nulls("date")
            .unique(subset=["date"], keep="last")
        )
    if not pieces:
        raise EmptyResponse("baidu_valuation", f"{market} {code} 的三个估值指标一条都没回")

    merged = pieces[0]
    for piece in pieces[1:]:
        merged = merged.join(piece, on="date", how="full", coalesce=True)
    columns = [column for column in frames if column in merged.columns]
    populated = merged.select(
        pl.sum_horizontal([pl.col(column).is_not_null() for column in columns]).sum()
    ).item()
    if not populated:
        raise EmptyResponse("baidu_valuation", f"{market} {code} 的三个估值指标全是空值")

    if "market_cap" in merged.columns:
        merged = merged.with_columns((pl.col("market_cap") * YI).alias("market_cap"))
    out = normalize_valuation(
        merged,
        market=market,
        code=code,
        source="baidu_valuation",
        column_map={name: name for name in merged.columns},
        percent_columns=(),
    )
    return out.filter(pl.col("date").is_between(start, end))


class BaiduValuationAdapter:
    name = "baidu_valuation"
    #: 美股估值没有第二个域（§4：百度的三种代码形态全失败），这一路只接 A 与港股
    markets = frozenset({"A", "HK"})

    def __init__(
        self,
        *,
        fetch: Callable[[str, str, str, str], pl.DataFrame] = baidu_valuation_indicator,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._fetch = fetch
        self._clock = clock

    def valuation(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        """按交易日的 PE(TTM)/PB/总市值。一只票三次请求；全空是 `EmptyResponse`。"""
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        if end < start:
            raise ValueError(f"区间反向: {start} > {end}")
        symbol = canonical_code(market, code)
        period = daily_period(self._clock.today(), start)
        frames: dict[str, pl.DataFrame] = {}
        try:
            for column, indicator in INDICATORS.items():
                frames[column] = self._fetch(market, symbol, indicator, period)
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc
        return parse_baidu_valuation(
            frames, market=market, code=code, start=start, end=end
        )


source(
    "baidu_valuation",
    note="百度股市通 A/港股按日估值（PE(TTM)/PB/总市值；逐日只到近三年）",
)(BaiduValuationAdapter)
