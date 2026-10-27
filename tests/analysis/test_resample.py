"""周线与月线由日线现算（spec §8.2）。

两条口径决定都写在这里，而不是留给读者猜：

1. **一根周 K 的名字是那个周期在日历上的收市日（周五 / 月末最后一天），不是这段里
   最后一根 K 线**。周五休市那一周仍然叫那个周五——否则「本周」的含义会随市场停电、
   长假漂移，而周线指标吃的正是「第 N 周」。实际最后一根另存一列，两者不一致就说明
   那一周提前收市。
2. **没走完的那一周不冒充收盘周 K**（`pending` 列）。周期收市日还在今天之后 ⇒ 这根
   周 K 是半成品，拿它算周线 MA 会把一个只走了三天的形态当成一整周。这里给的是标记
   而不是拒绝：读数的人常常就是要看本周走到哪了，但屏幕必须知道它还没走完。

复权在这里**不重新推导**：输入必须是 `adjusted_series()` 出来的那条已经带因子的序列，
重采样只是把已定型的日 K 聚合成周/月 K。自己重乘一遍因子的写法有两个（每日累乘、
每周期累乘），差多少取决于那几天涨了多少——那种「看起来正常」的序列正是 D42 要挡的。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import polars as pl
import pytest

from sams.analysis.adjust import AdjustedSeries, adjusted_series
from sams.analysis.indicators import ma
from sams.analysis.resample import resample
from sams.core.errors import ValidationError

START = date(2024, 1, 1)
#: 八周的周一至五（40 个工作日），够算周线 MA5 再留几天余量
WEEKDAYS = [
    d
    for d in (START + timedelta(days=i) for i in range(56))
    if d.weekday() < 5
]
END = WEEKDAYS[-1]

_ACTION_SCHEMA = {
    "ex_date": pl.Date,
    "kind": pl.Utf8,
    "cash_per_share": pl.Float64,
    "ratio": pl.Float64,
    "price": pl.Float64,
}


@dataclass(frozen=True)
class Span:
    covered_from: date = START
    covered_to: date = END
    full_history: bool = True
    src: str = "akshare"


def daily(
    days: list[date],
    value: float = 100.0,
    *,
    step: float = 0.0,
    actions: list[tuple[date, str, float, float | None, float | None]] = (),
) -> AdjustedSeries:
    closes = [value + step * i for i in range(len(days))]
    frame = pl.DataFrame(
        {
            "date": days,
            # 列集合与 `bars.load()` 的 `SELECT *` 一致：重采样要吃的就是真实形状，
            # 不是这里方便测试而挑出来的几列
            "market": ["US"] * len(closes),
            "code": ["AAPL"] * len(closes),
            "adjust": ["raw"] * len(closes),
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [100.0] * len(closes),
            "amount": [1000.0] * len(closes),
            "turnover": [1.0] * len(closes),
            "source": ["akshare"] * len(closes),
            "updated_at": [datetime(2024, 1, 1, 18)] * len(closes),
        }
    )
    fields = ("ex_date", "kind", "cash_per_share", "ratio", "price")
    rows = (
        pl.DataFrame([dict(zip(fields, a, strict=True)) for a in actions])
        if actions
        else pl.DataFrame(schema=_ACTION_SCHEMA)
    )
    return adjusted_series("US", "AAPL", frame, rows, coverage=[Span()], today=END)


# --- 分组口径 -----------------------------------------------------------------------


def test_a_week_is_named_after_friday_not_after_its_last_bar():
    """一周五天全在 ⇒ 周期收市日就是周五，也是最后一根。"""
    days = WEEKDAYS[:5]  # 周一到周五
    out = resample(daily(days), "week", as_of=END)
    assert out.frame["date"].to_list() == [date(2024, 1, 5)]
    assert out.frame["pending"].to_list() == [False]


def test_a_holiday_friday_does_not_move_the_week_bar():
    """周五休市：这根周 K 仍然叫那个周五，而「实际最后一根」是周四。

    两个字段一起给，读的人才分得清「这一周只开到周四」与「我把周界定错了」。
    """
    days = WEEKDAYS[:4]  # 周一到周四
    out = resample(daily(days), "week", as_of=END)
    assert out.frame["date"].to_list() == [date(2024, 1, 5)]
    assert out.frame["last"].to_list() == [date(2024, 1, 4)]


def test_days_are_grouped_by_week_across_a_month_boundary():
    """2024-01-29 到 2024-02-02 是同一个交易周，虽然它跨了月。

    按「月内第几周」或按自然月切周，会把这一周拆成两根，而周线 MA5 于是数到六根。
    """
    days = [d for d in WEEKDAYS if date(2024, 1, 29) <= d <= date(2024, 2, 2)]
    out = resample(daily(days), "week", as_of=END)
    assert out.frame.height == 1
    assert out.frame["date"].to_list() == [date(2024, 2, 2)]


def test_the_first_week_of_the_year_is_the_week_containing_the_first_bar():
    """1 月 1 日（周一）所在的那一周就是第一根周 K，不做 ISO 周年的跨年偏移。"""
    out = resample(daily(WEEKDAYS[:5]), "week", as_of=END)
    assert out.frame["date"].to_list() == [date(2024, 1, 5)]


def test_a_month_bar_is_named_after_the_last_day_of_that_month():
    days = [d for d in WEEKDAYS if d.month == 1]
    out = resample(daily(days), "month", as_of=END)
    assert out.frame["date"].to_list() == [date(2024, 1, 31)]
    assert out.frame["last"].to_list() == [date(2024, 1, 31)]


def test_a_quarter_bar_is_named_after_the_last_day_of_that_quarter():
    """季线沿用周月那条口径 1：名字是日历季末日，实际最后一根在 `last` 里。
    3-29 提前收市那一季仍然叫 3-31，否则「本季」会随长假漂移。

    整组日期比设计稿往前挪一年：`daily()` 那条覆盖区间止于 `END`（2024-02-23），
    2024 年 3 月那根日线过不了复权闸，出不了数。
    """
    days = [date(2023, 1, 3), date(2023, 2, 15), date(2023, 3, 28)]
    out = resample(daily(days), "quarter", as_of=END)
    assert out.frame["date"].to_list() == [date(2023, 3, 31)]
    assert out.frame["first"].to_list() == [date(2023, 1, 3)]
    assert out.frame["last"].to_list() == [date(2023, 3, 28)]


def test_a_year_bar_collects_every_quarter_of_that_year():
    """一年十二个月的日线聚成一根年 K，名字是 12-31（日期挪年的理由同上）。"""
    days = [date(2023, month, 15) for month in range(1, 13)]
    out = resample(daily(days), "year", as_of=END)
    assert out.frame["date"].to_list() == [date(2023, 12, 31)]
    assert out.frame["bars"].to_list() == [12]


def test_quarters_split_across_a_year_boundary():
    """跨年那一组分成的不是同一季：两根季 K，各自叫自己那季的日历末日。"""
    days = [date(2023, 12, 15), date(2024, 1, 15), date(2024, 2, 15)]
    out = resample(daily(days), "quarter", as_of=END)
    assert out.frame["date"].to_list() == [date(2023, 12, 31), date(2024, 3, 31)]


# --- 聚合口径 -----------------------------------------------------------------------


def test_ohlc_of_a_period_uses_the_first_open_last_close_and_the_extremes():
    days = WEEKDAYS[:5]
    out = resample(daily(days, value=100.0, step=1.0), "week", as_of=END).frame
    # closes 100,101,102,103,104；high=×1.01 low=×0.99
    bar = bar_at(out, 0)
    assert bar["open"] == pytest.approx(100.0)
    assert bar["close"] == pytest.approx(104.0)
    assert bar["high"] == pytest.approx(104.0 * 1.01)
    assert bar["low"] == pytest.approx(100.0 * 0.99)


def test_volume_and_amount_are_summed_not_averaged():
    out = resample(daily(WEEKDAYS[:5]), "week", as_of=END).frame
    bar = bar_at(out, 0)
    assert bar["volume"] == pytest.approx(500.0)
    assert bar["amount"] == pytest.approx(5000.0)


def test_the_adjusted_columns_are_resampled_from_the_adjusted_daily_bars():
    """`*_hfq` 由日线 `*_hfq` 聚合，`open/high/low/close` 由裸价聚合：两栏都对得上各自那侧。

    这条用例测的是「重采样不认识因子」这件事本身——它只做聚合，不做任何乘法。
    """
    series = daily(WEEKDAYS[:5])
    out = resample(series, "week", as_of=END).frame
    bar = bar_at(out, 0)
    assert bar["close_hfq"] == pytest.approx(bar["close"])
    assert bar["high_hfq"] == pytest.approx(bar["high"])


def test_turnover_sums_while_adjust_and_updated_at_carry_the_period_edge():
    """库里真实那一列集合要能过重采样，而且各自按自己的口径聚。

    `bars.load()` 是 `SELECT *`，所以换手率、`adjust`、`updated_at` 一定会出现在帧里：
    重采样不认识它们就整条拒掉，用户在命令行敲一次 `series` 就什么也拿不到。
    换手率是「当日成交量 / 流通股本」，五天买卖 5% 就是这一周换手 5%，所以按日相加；
    `adjust` 是读路径过滤出来的常数；`updated_at` 与 `source` 同口径取最后一根。
    """
    out = resample(daily(WEEKDAYS[:5]), "week", as_of=END).frame
    bar = bar_at(out, 0)
    assert bar["turnover"] == pytest.approx(5.0)
    assert bar["adjust"] == "raw"
    assert bar["updated_at"] == datetime(2024, 1, 1, 18)


def test_a_column_resample_does_not_understand_is_refused_not_dropped():
    """不认识的列要出声，不能静默少一栏。

    丢掉一列 `high_hfq` 与丢掉一列没有区别，而后者正是「看起来完全正常」的那种错法。
    """
    series = daily(WEEKDAYS[:5])
    mutated = AdjustedSeries(
        frame=series.frame.with_columns(pl.Series("vwap", [1.0] * series.frame.height)),
        covered_by=series.covered_by,
        covered_through=series.covered_through,
        factors=series.factors,
    )
    with pytest.raises(ValidationError, match="vwap"):
        resample(mutated, "week", as_of=END)


def test_an_ex_date_in_the_middle_of_a_week_stays_in_the_daily_bars():
    """除权在周中：日线已经把因子乘进去了，周线只把乘好的日线聚合起来。

    如果反过来（先聚合再乘），这根周 K 会整周用同一个因子，而周三之前那几天的价格
    本来不含这次除权——因子会被多乘一遍。这正是 D42 说的那两种写法差多少。
    """
    days = WEEKDAYS[:5]
    series = daily(days, value=100.0, step=1.0, actions=[(days[2], "dividend", 1.0, None, None)])
    out = resample(series, "week", as_of=END).frame
    daily_high_hfq = series.frame["high_hfq"].to_list()
    daily_close_hfq = series.frame["close_hfq"].to_list()
    assert out["close_hfq"].to_list()[0] == pytest.approx(daily_close_hfq[-1])
    assert out["high_hfq"].to_list()[0] == pytest.approx(max(daily_high_hfq))
    # 裸价那一栏同样各归各：聚合不改尺度
    assert out["close"].to_list()[0] == pytest.approx(104.0)


# --- 未完成的那一周 ----------------------------------------------------------------


def test_the_current_week_is_marked_pending():
    """今天是周三 ⇒ 本周这根周 K 还没走完，必须带 `pending`。

    少了这个标记，「周线 MA5」里会混进一根只走了三天的 K 线，而它在图上与一根完整
    周 K 完全同形。这是 D36「库里 0 行不等于没分红」的同一条道理在时间轴上的版本。
    """
    wednesday = date(2024, 1, 10)
    days = [d for d in WEEKDAYS if d <= wednesday]
    out = resample(daily(days), "week", as_of=wednesday).frame
    assert out["pending"].to_list() == [False, True]


def test_a_finished_week_is_not_pending_even_when_the_clock_is_later():
    days = [d for d in WEEKDAYS if d <= date(2024, 1, 5)]
    out = resample(daily(days), "week", as_of=END).frame
    assert out["pending"].to_list() == [False]


def test_a_week_whose_friday_is_after_the_clock_is_pending():
    """`as_of` 落在周一：那周的周五还没到，哪怕五天数据都在手里也还没收市。

    「收没收市」由时钟说了算，不由「库里有没有那根 K 线」说了算——后者在补采历史时
    会把每一周都误判成已完成。
    """
    days = [d for d in WEEKDAYS if date(2024, 1, 8) <= d <= date(2024, 1, 12)]
    out = resample(daily(days), "week", as_of=date(2024, 1, 8)).frame
    assert out["pending"].to_list() == [True]


# --- 入口与返回类型 ----------------------------------------------------------------


def test_resample_refuses_a_bare_frame():
    with pytest.raises(ValidationError, match="AdjustedSeries"):
        resample(daily(WEEKDAYS[:5]).frame, "week", as_of=END)


def test_resample_refuses_an_unknown_period():
    """`period` 只认 week/month：写成 "weekly" 不许静默当成 week。"""
    with pytest.raises(ValidationError, match="week"):
        resample(daily(WEEKDAYS[:5]), "weekly", as_of=END)


def test_the_resampled_series_can_feed_the_indicators_directly():
    """周线 MA5 吃重采样后的 `AdjustedSeries`：指标层不需要知道这根 K 是日线还是周线。"""
    out = resample(daily(WEEKDAYS[:5]), "week", as_of=END)
    assert isinstance(out, AdjustedSeries)
    ma5 = ma(out, 3)
    assert ma5.height == 1


def test_the_carried_provenance_survives_resampling():
    """`covered_by`/`covered_through`/`factors` 一起带过去：重采样不改变「谁为这段背书」。

    少了这些，周线那条路上就没人知道这段历史是谁确认的（D49 换来的可审计性会在
    这里断掉，而断掉的样子正是「看不出来断了」）。
    """
    series = daily(WEEKDAYS[:5])
    out = resample(series, "week", as_of=END)
    assert out.covered_by == series.covered_by
    assert out.covered_through == series.covered_through
    assert out.factors == series.factors


def bar_at(frame: pl.DataFrame, index: int) -> dict:
    return frame.slice(index, 1).to_dicts()[0]
