"""区间涨跌：能算才算，算不出就说原因（spec §18 U12 / D36 / D37）。

裸价跨除权日的区间涨跌是**一个看起来完全正常的错数**：曲线照样画得出、指标照样算得出、
只有收益是错的。601088 在 2024-07-08 用裸价算是 −3.81%，交易所公布 +1.26%，
差额就是那天的除息。这一层的唯一职责是让那种数出不了门。

判定本身都在 `analysis/guard.py`，因为这个文件只做一件额外的活：把过了闸的序列
取首尾两根作比。守卫放这里而不是只放这里，是因为指标引擎走的是同一个闸
（`adjust.adjusted_series`），两道闸不可能一个调用方记得、另一个忘了。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import polars as pl

from sams.analysis.adjust import adjusted_series
from sams.analysis.guard import ConfirmedSpan
from sams.core.errors import NotAdjustable


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
    listed_on: date | None = None,
) -> IntervalReturn:
    """[start, end] 的区间涨跌。拒绝时抛 `NotAdjustable` 并带上原因。

    `bars` 可以只取到 `start` 附近：本函数只取窗口内的首尾两根，而除权因子只吃
    落在这两根之间的记录（更早的因子对两端是同一个常数，见 `adjust.actions_in_span`）。
    `listed_on` 是上市首日——不传就不给「上市初期不设涨跌幅」那条形豁免，
    因为豁免的位置一旦按请求窗口算，一只老票的窄窗口就能把 −30% 的不可能跳变放出去。
    """
    window = bars.filter((pl.col("date") >= start) & (pl.col("date") <= end)).sort("date")
    if window.height < 2:
        raise NotAdjustable(f"{market}/{code} 在 {start}~{end} 不足两根 K 线，无从比较")
    _require_window_backed_by_bars(market, code, window, start, end)
    series = adjusted_series(
        market,
        code,
        window,
        actions,
        coverage=coverage,
        today=today,
        listed_on=listed_on,
        price_columns=("close",),
    )
    first, last = series.frame["date"].to_list()[0], series.frame["date"].to_list()[-1]
    raw = _ratio(series.frame, first, last, "close")
    hfq = _ratio(series.frame, first, last, "close_hfq")
    return IntervalReturn(
        market=market,
        code=code,
        start=first,
        end=last,
        raw=raw,
        adjusted=hfq,
        ex_dates=series.ex_dates,
        covered_by=series.covered_by,
    )


def _ratio(frame: pl.DataFrame, start: date, end: date, column: str) -> float:
    rows = frame.filter(pl.col("date").is_in([start, end]))[column].to_list()
    if len(rows) != 2 or not rows[0]:
        raise NotAdjustable(f"{column} 在 {start} 或 {end} 缺失/为零，区间涨跌无从计算")
    return rows[1] / rows[0] - 1


#: 请求边上的这几天缺口是常态（周末、长假、短暂停牌），不算「这段没数据」。
#: 15 个日历日够覆盖最长连休加一周停牌，又远小于「少了半个月」那种真缺口。
_ANCHOR_GRACE_DAYS = 15


def _require_window_backed_by_bars(
    market: str, code: str, window: pl.DataFrame, start: date, end: date
) -> None:
    """两端必须有 K 线，否则这个数就不是请求的那个区间的数。

    `IntervalReturn.start/end` 一直诚实地带着真实锚点，但读的人知道自己请求了什么、
    不知道库里有什么：请求一整年、库里只有一个半月，他看见的就是一个「年度涨跌」。
    少这一段数据不会让数字看起来异常，只会让它安静地变成另一个区间的数。
    """
    first, last = window["date"].to_list()[0], window["date"].to_list()[-1]
    missing_before = (first - start).days
    missing_after = (end - last).days
    if missing_before <= _ANCHOR_GRACE_DAYS and missing_after <= _ANCHOR_GRACE_DAYS:
        return
    if missing_before > _ANCHOR_GRACE_DAYS:
        head, gap = "起点", missing_before
    else:
        head, gap = "终点", missing_after
    raise NotAdjustable(
        f"{market}/{code} 请求 {start}~{end}，但库里的 K 线只覆盖 {first}~{last}"
        f"（{head}之外缺 {gap} 天）：这段没同步到，不能按 {start}~{end} 给涨跌。"
        f"先 `sams sync daily --market {market} --code {code} --start {start} --end {end}`，"
        f"或把区间收到 {first}~{last} 以内"
    )
