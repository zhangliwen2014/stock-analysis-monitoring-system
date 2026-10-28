"""分钟粒度：分时与分钟 K 的读取面（D163/D164/D165）。

三条职责，都只读、都不落库：

  1. `availability`——这一市场的两档谁能答、不能答时怎么说。周期条上的置灰与
     接口返回的 `note` 共用这一份，屏上和 JSON 里不会各说一套（D165）。
  2. `timeshare`——把腾讯给的**累计**量差分回每分钟，并算出均价线。
  3. `bars`——把分钟 K 组装成 `AdjustedSeries` 交给 `analysis.metrics`（D164）。

缓存的是**取回来的帧**而不是算好的表：换一组指标勾选不该再打一次外网，
而把 `metrics` 的结果缓存下来会让「同一份数两种指标」在缓存里并存。
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

import polars as pl

from sams.analysis.adjust import PRICE_COLUMNS, AdjustedSeries
from sams.analysis.indicators import metrics
from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import CalendarSourceError, DatasourceError, ValidationError
from sams.datasource.capabilities import capability
from sams.datasource.minute import answers

#: 屏幕上那六档分钟 K：键 = URL 里那个 `period`，值 = 适配器要的 `scale`（分钟数）
MINUTE_SCALES: dict[str, int] = {
    "m1": 1,
    "m5": 5,
    "m15": 15,
    "m30": 30,
    "m60": 60,
    "m120": 120,
}

#: 一次取多少根：一个交易日 240 分钟，`m1` 刚好一整天，`m120` 是两天
MINUTE_BARS = 240
#: 分钟数据只覆盖最近约一年（腾讯实测 `202610191405` 起）：窗口给到 365 天就是给到底
MINUTE_LOOKBACK_DAYS = 365
#: 一次读数到下一次打外网之间的间隔：分时是「现在」的数，缓存久了就不是了
DEFAULT_TTL_S = 60.0
#: 缓存的条数上限：一屏一张图，400 张够一个交易日里来回点很多次
_CACHE_CAP = 400


@dataclass(frozen=True)
class KindAnswer:
    """一档（分时 / 分钟 K）在某个市场的可用性。"""

    label: str
    ok: bool
    #: 链上有这个方法但关掉了这一市场的源：`(源名, 原因)`
    refused: tuple[tuple[str, str], ...] = ()

    @property
    def why_not(self) -> str:
        """不能答时的那句话。

        两种「不能」必须分开：链上根本没有这个方法的源（配置里就没接），
        与有方法但这个市场不答（接了，腾讯不吃美股的分时）——后者要念出源名，
        否则读的人以为是自己没配。
        """
        if self.refused:
            names = "、".join(name for name, _reason in self.refused)
            return f"{names} 不供这一市场的{self.label}"
        return f"这条链上没有能提供{self.label}的源"


@dataclass(frozen=True)
class MinuteAvailability:
    market: str
    series: KindAnswer
    bars: KindAnswer


@dataclass(frozen=True)
class MinuteBarsRead:
    """分钟 K 的一次读取：`series` 交给指标，`table` 是算完指标的那张宽表。

    拒答时 `series`/`table` 都是 `None` 而 `note` 有那句话——接口层只认 `note`，
    不许把 `None` 当「这段没数据」画成空图。
    """

    period: str
    series: AdjustedSeries | None
    table: pl.DataFrame | None
    source: str
    note: str | None = None

    @classmethod
    def refusal(cls, period: str, note: str) -> MinuteBarsRead:
        return cls(period=period, series=None, table=None, source="", note=note)


@dataclass(frozen=True)
class Timeshare:
    """一天分时：`times` 与三条序列同长同序，`volume` 已经是每分钟的量。"""

    day: date | None
    times: tuple[str, ...]
    price: tuple[float, ...]
    avg: tuple[float | None, ...]
    volume: tuple[float, ...]
    prev_close: float | None
    source: str
    note: str | None = None

    @classmethod
    def refusal(cls, note: str) -> Timeshare:
        return cls(
            day=None,
            times=(),
            price=(),
            avg=(),
            volume=(),
            prev_close=None,
            source="",
            note=note,
        )


class MinuteService:
    def __init__(
        self,
        registry,
        calendar,
        *,
        clock: Clock = SYSTEM_CLOCK,
        ttl_s: float = 60.0,
        now=time.monotonic,
    ) -> None:
        self._registry = registry
        self._calendar = calendar
        self._clock = clock
        self._ttl = ttl_s
        self._now = now
        #: `(market, code, period)` → `(装填时刻, 帧)`：见模块 docstring
        self._cache: dict[tuple[str, str, str], tuple[float, object]] = {}

    def availability(self, market: str) -> MinuteAvailability:
        return MinuteAvailability(
            market,
            self._answer(market, "minute_series"),
            self._answer(market, "minute_bars"),
        )

    def _answer(self, market: str, method: str) -> KindAnswer:
        """`chain_for`（有没有这个方法）× `answers`（答不答这个市场）——两问都得问。

        只查前者会把「腾讯有 minute_bars 但美股不答」算成可用：置灰失效，
        点下去收到的是一句接口错误而不是「这一档不供美股」。
        """
        cap = capability(method)
        chain = self._registry.chain_for(market, method)
        willing: list[str] = []
        refused: list[tuple[str, str]] = []
        for name in chain:
            adapter = self._registry.adapter(name)
            if answers(adapter, method, market):
                willing.append(name)
            else:
                refused.append((name, f"{cap.label}不答 {market}"))
        return KindAnswer(cap.label, bool(willing), tuple(refused))

    def timeshare(self, market: str, code: str) -> Timeshare:
        """一次只答一天：接口本身也只有「最近一个交易日」那一条路（Task 3 的区间校验）。"""
        answer = self._answer(market, "minute_series")
        if not answer.ok:
            return Timeshare.refusal(answer.why_not)
        today = self._clock.today()
        try:
            result = self._registry.fetch_minute_series(market, code, today)
        except DatasourceError as exc:
            return Timeshare.refusal(f"{today} 的分时取不出来：{exc}")
        if result.empty_everywhere:
            return Timeshare.refusal(f"{today} 没有分时：这条链上每个源都答了「没有那一天」")
        frame = self._trim_to_session(result.frame, market, today)
        times = list(frame["time"].to_list())
        if not times:
            return Timeshare.refusal(f"{today} 的分时一行有效数据都没有（可能还没开盘）")
        cum_volume = frame["volume"].to_list()
        cum_amount = frame["amount"].to_list()
        # 每分钟量 = 累计的一阶差分：第一项没有前态可减，就是它自己（源给的是累计，D171）
        volume = [cum_volume[0], *(later - earlier for earlier, later in pairwise(cum_volume))]
        # 均价：分子分母都是累计值，直接相除；累计量为 0 的那一分钟除不出数，交 null
        avg = [
            (amount / cumulative) if cumulative else None
            for amount, cumulative in zip(cum_amount, cum_volume, strict=True)
        ]
        return Timeshare(
            day=frame["date"].to_list()[0],
            times=tuple(times),
            price=tuple(frame["price"].to_list()),
            avg=tuple(avg),
            volume=tuple(volume),
            prev_close=frame["prev_close"].to_list()[0],
            source=str(frame["source"][0]),
        )

    def _trim_to_session(self, frame: pl.DataFrame, market: str, day: date) -> pl.DataFrame:
        """裁掉收市之后的行：港股实测会报到 16:08，A 股全天 267 行里含 `15:00` 之后那一段。

        裁的依据是日历给的当日**最晚**收市时刻（午休与市场自己的收盘不是一个东西），
        而日历答不上来时原样交回——悄悄删数据比留着几根晚到的分钟坏得多。
        """
        if self._calendar is None:
            return frame
        try:
            sessions = self._calendar.sessions(market, day)
        except CalendarSourceError:
            return frame
        closes = [session.close for session in sessions if session.close]
        if not closes:
            return frame
        cutoff = max(closes)
        # 收市时刻是市场本地时的绝对时刻，`time` 是零填充的 "HH:MM"：比到同一面墙上就是比时刻
        cut = cutoff.strftime("%H:%M")
        trimmed = frame.filter(pl.col("time") <= cut)
        return trimmed if not trimmed.is_empty() else frame

    def bars(
        self,
        market: str,
        code: str,
        *,
        period: str,
        start: date,
        end: date,
        indicators: Sequence[str] = (),
        windows: Mapping[str, Sequence[int]] | None = None,
    ) -> MinuteBarsRead:
        scale = MINUTE_SCALES.get(period)
        if scale is None:
            raise ValidationError(
                f"{period} 不是分钟档：可选的是 {', '.join(MINUTE_SCALES)}"
            )
        answer = self._answer(market, "minute_bars")
        if not answer.ok:
            return MinuteBarsRead.refusal(period, answer.why_not)
        frame = self._frame(market, code, period, scale, start, end)
        if isinstance(frame, str):
            return MinuteBarsRead.refusal(period, frame)
        series = _assemble(frame, str(frame["source"][0]))
        table = metrics(series, tuple(indicators) or ("ma",), windows=windows)
        # `vol` 那一格画的是裸量柱（`INDICATORS` 里没有 `vol`），指标表得自己带上这一列
        return MinuteBarsRead(
            period, series, table.with_columns(frame["volume"]), series.covered_by
        )

    def _frame(
        self, market: str, code: str, period: str, scale: int, start: date, end: date
    ) -> pl.DataFrame | str:
        """取这一档最近 `MINUTE_BARS` 根，并按 `[start, end]` 裁；失败与「都不答」返回一句 note。"""
        key = (market, code, period)
        hit = self._cache.get(key)
        now = self._now()
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1].filter(pl.col("date").is_between(start, end))
        try:
            result = self._registry.fetch_minute_bars(
                market, code, start, end, scale=scale, count=MINUTE_BARS
            )
        except DatasourceError as exc:
            return f"{period} 的分钟K取不出来：{exc}"
        if result.empty_everywhere:
            # 读列名之前必须先看这个旗标：那时 `frame` 装的是**日线**列集的 `EMPTY_BARS`
            return f"{period} 的分钟K这一段没有数：每个源都答了「没有」"
        self._remember(key, (now, result.frame))
        return result.frame.filter(pl.col("date").is_between(start, end))

    def _remember(self, key: tuple[str, str, str], entry: tuple[float, pl.DataFrame]) -> None:
        """超上限就按插入顺序丢最旧的：分钟数据只活一天，精确的 LRU 不值得那圈记账。"""
        if len(self._cache) >= _CACHE_CAP and key not in self._cache:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = entry


def _assemble(frame: pl.DataFrame, source: str) -> AdjustedSeries:
    """把分钟帧组装成 `AdjustedSeries`：`adj_factor` 恒为 1（D164）。

    不借 `analysis.adjust.hfq_frame`：那条路要 `day_factors`，而分钟档既没有除权日可跳、
    也没有日线覆盖确认可查——闸的前提在这一档不成立，不是有人把闸绕开了。
    逐档价格都要出 `_hfq` 列：只给 `close_hfq` 等于逼每个指标自己再乘一遍，
    最容易的错法是把裸 high/low 直接喂进 ATR、BOLL、KDJ。
    """
    ordered = frame.sort(["date", "clock"])
    moments = pl.concat_str(
        [
            pl.col("date").cast(pl.Utf8),
            pl.lit(" "),
            # "11:05" → "11:05:00"：`%H:%M` 在 DuckDB/polars 那侧的解析比带秒脆
            pl.col("clock") + pl.lit(":00"),
        ]
    ).str.strptime(pl.Datetime, "%Y-%m-%d %H:%M:%S")
    priced = ordered.with_columns(moments.alias("date")).drop("clock")
    adj = pl.Series("adj_factor", [1.0] * priced.height)
    columns = [name for name in PRICE_COLUMNS if name in priced.columns]
    hfq = [(pl.col(name) * adj).alias(f"{name}_hfq") for name in columns]
    out = priced.with_columns(adj, *hfq)
    days = out["date"].to_list()
    return AdjustedSeries(
        frame=out,
        covered_by=source,
        covered_through=days[-1].date(),
        factors=(),
    )
