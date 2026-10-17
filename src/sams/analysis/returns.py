"""区间涨跌：能算才算，算不出就说原因（spec §18 U12 / D36 / D37）。

裸价跨除权日的区间涨跌是**一个看起来完全正常的错数**：曲线照样画得出、指标照样算得出、
只有收益是错的。601088 在 2024-07-08 用裸价算是 −3.81%，交易所公布 +1.26%，
差额就是那天的除息。这一层的唯一职责是让那种数出不了门。

两道判定：

* **覆盖（D36）**：`corp_actions` 里 0 行既可能是「从没分过红」也可能是「没查过」。
  只有采集器留下过确认记录，才有资格说「这个区间里没有除权」。
* **证据（D37）**：A 股裸价出现超过该板块最宽涨跌幅的跳变 ⇒ 一定有没记录的行为 ⇒
  即使覆盖标记说确认过也拒绝。采集器漏一条是常态，不能让它骗过守卫。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

import polars as pl

from sams.analysis.adjust import hfq_frame
from sams.core.codes import daily_price_limit
from sams.core.errors import NotAdjustable

#: 上市初期的跳变不作证据：主板首日、双创前五日不设涨跌幅，那种大阳线与除权长得一样
_SKIP_LEADING_BARS = 5


class ConfirmedSpan(Protocol):
    """`storage.repos.corp_actions_coverage.CoverageRecord` 的读法视图。

    这里用 Protocol 而不是直接 import 那个类：分析层不该依赖仓库层的具体类型，
    而「一段被确认过的区间」这件事本身只有四个字段。
    """

    covered_from: date
    covered_to: date
    full_history: bool
    src: str


@dataclass(frozen=True)
class IntervalReturn:
    market: str
    code: str
    start: date
    end: date
    raw: float
    adjusted: float
    #: 区间内参与计算的除权日个数。0 时 raw 与 adjusted 必然相等——这是可以直说的巧合，
    #: 而不是「复权没影响结果」
    ex_dates: int
    covered_by: str | None

    @property
    def differs(self) -> bool:
        return abs(self.raw - self.adjusted) > 1e-9


def is_covered(
    spans: Sequence[ConfirmedSpan], *, start: date, end: date, today: date | None = None
) -> ConfirmedSpan | None:
    """这个区间的除权记录是否被某个源完整确认过。

    全历史确认只看上界：源说「这只票的历史我给全了，截至 covered_to」，那么
    covered_to 之前的每一天都有答案，包括它派息之前的年份。
    """
    for span in spans:
        if span.full_history:
            if span.covered_to >= end and (today is None or span.covered_to >= today):
                return span
        elif span.covered_from <= start and span.covered_to >= end:
            return span
    return None


def interval_return(
    bars: pl.DataFrame,
    actions: pl.DataFrame,
    *,
    market: str,
    code: str,
    start: date,
    end: date,
    coverage: Sequence[ConfirmedSpan] = (),
    today: date | None = None,
) -> IntervalReturn:
    """[start, end] 的区间涨跌。拒绝时抛 `NotAdjustable` 并带上原因。

    `bars` 需要含 `start` 之前的数据也无妨：本函数只取窗口内的首尾两根，但因子由
    全量传入的 `bars` 推（除权参考价要用「除权日前一根」，窗口本身可能正好从
    除权日开头）。
    """
    window = bars.filter((pl.col("date") >= start) & (pl.col("date") <= end)).sort("date")
    if window.height < 2:
        raise NotAdjustable(f"{market}/{code} 在 {start}~{end} 不足两根 K 线，无从比较")
    _reject_unmarked_ex_dates(market, code, window)
    span = is_covered(coverage, start=start, end=end, today=today)
    if span is None:
        raise NotAdjustable(
            f"{market}/{code} 的 {start}~{end} 没有公司行为覆盖确认："
            "`corp_actions` 里 0 行既可能是这只票从没分过红，也可能是没查过它，"
            "裸价区间涨跌在第二种情况下是错数。先跑 `sams sync actions --market "
            f"{market} --code {code}`"
        )
    adjusted = hfq_frame(bars, actions)
    first, last = window["date"].to_list()[0], window["date"].to_list()[-1]
    raw = _ratio(adjusted, first, last, "close")
    hfq = _ratio(adjusted, first, last, "close_hfq")
    ex_dates = actions.filter(
        (pl.col("ex_date") > first) & (pl.col("ex_date") <= last)
    ).height
    return IntervalReturn(
        market=market,
        code=code,
        start=first,
        end=last,
        raw=raw,
        adjusted=hfq,
        ex_dates=ex_dates,
        covered_by=span.src,
    )


def _ratio(frame: pl.DataFrame, start: date, end: date, column: str) -> float:
    rows = frame.filter(pl.col("date").is_in([start, end]))[column].to_list()
    if len(rows) != 2 or not rows[0]:
        raise NotAdjustable(f"{column} 在 {start} 或 {end} 缺失/为零，区间涨跌无从计算")
    return rows[1] / rows[0] - 1


def _reject_unmarked_ex_dates(market: str, code: str, window: pl.DataFrame) -> None:
    """D37：超过该板块最宽涨跌幅的裸价跳变 = 一定有没记录的公司行为。"""
    limit = daily_price_limit(market, code)
    if limit is None:
        return
    closes = window["close"].to_list()
    dates = window["date"].to_list()
    for i in range(_SKIP_LEADING_BARS + 1, len(closes)):
        prev, today = closes[i - 1], closes[i]
        if not prev:
            continue
        move = today / prev - 1
        # 放宽 1.5 倍：涨跌停价是按 0.01 元取整的，低价股的实际涨幅会略超名义比例
        if abs(move) > limit * 1.5:
            raise NotAdjustable(
                f"{market}/{code} 在 {dates[i]} 裸价变动 {move:+.1%}，超过该板块涨跌幅上限 "
                f"{limit:.0%} 的 1.5 倍：这一天一定有未记录的公司行为，"
                "区间涨跌不能按裸价给"
            )
