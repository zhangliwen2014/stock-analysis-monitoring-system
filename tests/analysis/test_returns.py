"""区间涨跌的两道守卫（D36 覆盖 / D37 证据）。

这里最要紧的用例不是「算得对」，而是「该拒绝时真的拒绝」：一个看起来正常的错数
会比算不出来糟糕得多——算不出还有人去查，错数会被直接拿去决策。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
from datetime import date, timedelta

import polars as pl
import pytest

from sams.analysis.guard import is_covered
from sams.analysis.returns import IntervalReturn, interval_return
from sams.core.errors import NotAdjustable

START = date(2024, 7, 1)
MID = date(2024, 7, 8)
END = date(2024, 7, 15)


@dataclass(frozen=True)
class Span:
    covered_from: date
    covered_to: date
    full_history: bool = True
    src: str = "akshare"


def frame(closes, days=None) -> pl.DataFrame:
    days = days or [START + timedelta(days=i) for i in range(len(closes))]
    return pl.DataFrame(
        {"date": days, "close": list(closes), "market": "A", "code": "601088"}
    )


def actions(*rows) -> pl.DataFrame:
    schema = {
        "ex_date": pl.Date,
        "kind": pl.Utf8,
        "cash_per_share": pl.Float64,
        "ratio": pl.Float64,
        "price": pl.Float64,
    }
    if not rows:
        return pl.DataFrame(schema=schema)
    cols = tuple(schema)
    return pl.DataFrame([dict(zip(cols, row, strict=True)) for row in rows])


def covered(end=END, *, full=True, start=START) -> list[Span]:
    return [Span(covered_from=start, covered_to=end, full_history=full)]


def get(**kwargs):
    """默认给一份「已确认覆盖 + 无除权」的正常输入，让每个用例只改自己那一条。"""
    kwargs.setdefault("bars", frame([10.0] * 11))
    kwargs.setdefault("actions", actions())
    kwargs.setdefault("market", "A")
    kwargs.setdefault("code", "601088")
    kwargs.setdefault("start", START)
    kwargs.setdefault("end", END)
    kwargs.setdefault("coverage", covered())
    return interval_return(**kwargs)


# --- D36：覆盖标记 ---------------------------------------------------------------


def test_a_window_without_coverage_is_refused_rather_than_reported_as_raw():
    with pytest.raises(NotAdjustable, match="从没分过红"):
        get(coverage=[])


def test_full_history_confirmation_covers_the_whole_earlier_past():
    """源说「历史给全了」，那么最早的年份也有答案，不必从区间第一天开始确认。"""
    result = get(coverage=[Span(covered_from=date(2020, 1, 1), covered_to=END)])
    assert result.covered_by == "akshare"


def test_a_partial_confirmation_does_not_cover_a_wider_window():
    with pytest.raises(NotAdjustable, match="覆盖确认"):
        get(
            coverage=[
                Span(covered_from=START, covered_to=date(2024, 7, 10), full_history=False)
            ]
        )


def test_today_after_the_confirmation_expires_a_full_history_claim():
    """确认是过去某天做的。今天已经更晚，那之后的除权没人看过。"""
    with pytest.raises(NotAdjustable, match="覆盖确认"):
        get(coverage=[Span(covered_from=START, covered_to=END)], today=END + timedelta(days=1))


def test_a_stale_full_history_span_never_qualifies():
    span = Span(covered_from=START, covered_to=date(2024, 7, 20), full_history=True)
    assert is_covered([span], start=START, end=END, today=date(2025, 1, 1)) is None


# --- D37：超涨跌停的跳变 ---------------------------------------------------------


def test_an_impossible_main_board_move_is_refused_even_with_coverage():
    """主板不可能一天跌 30%。有覆盖标记也拒绝——采集器漏一条是常态。"""
    closes = [10.0] * 8 + [7.0] + [7.0] * 3
    with pytest.raises(NotAdjustable, match="未记录的公司行为"):
        get(bars=frame(closes), actions=actions())


def test_a_limit_down_move_is_not_refused():
    """跌停本身就是 10%，把它当成「有未记录的除权」会把最正常的行情拒掉。"""
    closes = [10.0] * 8 + [9.0] + [9.0] * 3
    assert get(bars=frame(closes)).adjusted == pytest.approx(-0.10, abs=1e-9)


def test_new_listing_jumps_are_exempted_only_when_the_caller_says_when():
    """上市初期的无限制波动与除权在价格上长得一样，按「距上市第几根」跳过。

    豁免必须挂在 `listed_on` 上而不是「请求窗口的第几根」上：后者等于让调用方用一次
    窄窗口取数就买到一张免检票——那只 2007 年上市的票并没有上市首日。
    """
    closes = [10.0, 30.0, 45.0, 60.0, 75.0, 90.0, 85.0, 80.0, 82.0, 83.0, 84.0]
    assert get(bars=frame(closes), listed_on=START).raw == pytest.approx(84 / 10 - 1)
    # 同一段行情，不告诉守卫上市日，就不给豁免
    with pytest.raises(NotAdjustable, match="未记录的公司行为"):
        get(bars=frame(closes))


def test_a_window_starting_much_later_still_exempts_only_the_listing_bars():
    """给了上市日之后，豁免跟着「距上市第几根」走：老日子的不可能跳变一个都跑不掉。"""
    days = [START + timedelta(days=i) for i in range(120)]
    closes = [10.0] * 119 + [7.0]
    with pytest.raises(NotAdjustable, match="未记录的公司行为"):
        get(
            bars=frame(closes, days),
            listed_on=START,
            start=days[115],
            end=days[119],
            coverage=covered(end=days[119]),
        )


def test_markets_without_price_limits_rely_on_coverage_only():
    """港股没有涨跌停，这条证据不存在；没有覆盖标记同样拒绝，不放宽。"""
    closes = [10.0] * 8 + [4.0] + [4.0] * 3
    with pytest.raises(NotAdjustable, match="覆盖确认"):
        get(market="HK", code="00700", bars=frame(closes), coverage=[])


# --- 复权后的区间涨跌 -------------------------------------------------------------


def test_the_601088_case_raw_says_minus_3_81_percent_while_the_exchange_said_plus_1_26():
    """专家复核 #1 的回归用例。

    除息日：前收 42.00、每股派 2.10 → 参考价 39.90，当天收 40.40。
    裸价读作 −3.81%，交易所的除权口径读作 +1.25%。两个数描述的是同一天，
    只有一个是真的——所以这里必须同时断言两者，证明守卫给出的是后者。
    """
    days = [START + timedelta(days=i) for i in range(11)]
    closes = [41.00, 41.20, 41.40, 41.60, 41.80, 42.00, 42.00, 40.40, 41.00, 41.40, 42.00]
    result = get(
        bars=frame(closes, days),
        actions=actions((days[7], "dividend", 2.10, None, None)),
        start=days[6],
        end=days[7],
    )
    assert result.raw == pytest.approx(40.40 / 42.00 - 1, abs=1e-9)
    assert result.raw == pytest.approx(-0.0381, abs=1e-4)
    assert result.adjusted == pytest.approx(0.0125, abs=1e-4)
    assert result.ex_dates == 1
    assert result.differs, "不复权与复权必须给出不同的数，否则这条用例什么都没测到"


def test_a_window_with_no_ex_date_gives_the_same_number_both_ways():
    """没有除权时两者相等是**巧合**，用例锁住它，防止将来哪天因子算错还被当成一致。"""
    result = get(bars=frame([10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 13.0, 13.5, 14.0, 14.5, 15.0]))
    assert result.raw == pytest.approx(result.adjusted)
    assert result.ex_dates == 0
    assert not result.differs


def test_ex_date_on_the_first_bar_of_the_window_does_not_count():
    """除权日就是区间第一天时，那天的价格已经是除权后的，不该再算进区间。

    窗口外那一天不是白给的：除权参考价要用「前一根 K 线」，第一天就除权的序列
    必须在窗口之前还留着一根，否则连因子都推不出来。
    """
    days = [START - timedelta(days=1)] + [START + timedelta(days=i) for i in range(11)]
    closes = [42.00] + [39.90] * 4 + [40.20] * 7
    result = get(
        bars=frame(closes, days),
        actions=actions((days[1], "dividend", 2.10, None, None)),
    )
    assert result.ex_dates == 0


def test_fewer_than_two_bars_in_the_window_is_refused():
    with pytest.raises(NotAdjustable, match="不足两根"):
        get(start=END, end=END)


def test_result_is_frozen_and_carries_its_own_reason():
    result = get()
    assert isinstance(result, IntervalReturn)
    with pytest.raises(FrozenInstanceError):
        result.raw = 0.5  # type: ignore[misc]


def test_an_ex_date_before_the_window_needs_no_bars_from_that_day():
    """首根之前的除权对首尾之比是同一个常数：不该逼调用方取出自上市以来的 K 线。

    这条断言的另一种写法是「少取一段历史就报『库里没有前一根 K 线』」，那是
    `sams sync daily --start` 之后随手的区间查询最容易踩到的坑。
    """
    bars = frame([45.10, 45.20, 45.30, 45.40, 45.50, 45.60, 45.70, 45.80, 42.80, 43.00, 43.30])
    result = get(
        bars=bars,
        actions=actions((date(2008, 6, 4), "dividend", 2.55, None, None)),
    )
    assert result.ex_dates == 0
    assert result.raw == pytest.approx(result.adjusted)


def test_the_ex_date_on_the_first_bar_of_the_window_is_not_double_counted():
    """首根已经是除权后的价格：这一天的事件不该再进区间之比。"""
    ex_day = START
    result = get(
        bars=frame([42.80, 43.00, 43.30, 43.40, 43.50, 43.60, 43.70, 43.80, 44.00, 44.10, 44.20]),
        actions=actions((ex_day, "dividend", 2.26, None, None)),
    )
    assert result.ex_dates == 0
    assert result.adjusted == pytest.approx(result.raw)


# --- 请求窗口与读数锚点 -----------------------------------------------------------


def test_a_window_the_bars_do_not_really_cover_is_refused_not_answered_for_a_shorter_one():
    """请求一整年、库里只有一个半月 ⇒ 绝不能给数：那个数会顶着「年度涨跌」的名字出门。

    交易员试用报告里最伤的一条：`probe A 601088 2024-01-02 2024-12-31` 返回 −6.24%，
    实际算的是 06-20~07-31。`IntervalReturn.start/end` 诚实地带着真实锚点，但读的人
    知道自己请求了什么、不知道库里有什么，于是他看见的就是一个年度数。
    """
    days = [date(2024, 6, 20) + timedelta(days=i) for i in range(30)]
    with pytest.raises(NotAdjustable, match="只覆盖"):
        get(
            bars=frame([10.0] * 30, days=days),
            start=date(2024, 1, 2),
            end=date(2024, 12, 31),
        )


def test_a_window_starting_after_the_last_bar_is_refused_too():
    """尾端缺一样危险：拿 1 月的价格当「到 7 月为止的涨跌」报出去。"""
    days = [date(2024, 1, 2) + timedelta(days=i) for i in range(11)]
    with pytest.raises(NotAdjustable, match="只覆盖"):
        get(
            bars=frame([10.0] * 11, days=days),
            start=date(2024, 1, 2),
            end=date(2024, 7, 1),
        )


def test_a_holiday_or_suspension_at_the_edge_of_the_window_is_not_a_refusal():
    """边上的几天缺口是常态（长假、短暂停牌），不能把它当成「这段没数据」。

    阈值因此取 15 个日历日：够覆盖最长的连休加一周停牌，又远小于「少了半个月」。
    """
    result = get(bars=frame([10.0] * 11), start=START, end=END)
    assert result.start == START
    assert result.end == date(2024, 7, 11)
