from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from sams.core.errors import ValidationError
from sams.core.timeutil import (
    CalendarDay,
    SessionWindow,
    day_sessions,
    find_active_session,
    gap_days,
    parse_hm,
    post_close_run_at,
)


def D(day: int) -> date:
    return date(2024, 1, day)

CN = CalendarDay(
    market="A",
    day=date(2026, 3, 9),
    is_trading=True,
    is_half_day=False,
    tz="Asia/Shanghai",
    windows=(SessionWindow("am", "09:30", "11:30"), SessionWindow("pm", "13:00", "15:00")),
)
US_SUMMER_DAY = date(2026, 3, 9)
US_WINTER_DAY = date(2026, 3, 6)


def us_day(day: date) -> CalendarDay:
    return CalendarDay(
        market="US",
        day=day,
        is_trading=True,
        is_half_day=False,
        tz="America/New_York",
        windows=(SessionWindow("regular", "09:30", "16:00"),),
    )


def test_parse_hm_rejects_non_clock_strings():
    assert parse_hm("09:30") == parse_hm(" 09:30 ")
    with pytest.raises(ValidationError, match="HH:MM"):
        parse_hm("9:30 AM")


def test_non_trading_day_has_no_sessions():
    off = CalendarDay("A", date(2026, 10, 1), False, False, "Asia/Shanghai", ())
    assert day_sessions(off) == []


def test_cn_sessions_are_sorted_and_local():
    sessions = day_sessions(CN)
    assert [s.name for s in sessions] == ["am", "pm"]
    assert sessions[0].open.astimezone(UTC).hour == 1  # 09:30 CST = 01:30 UTC
    assert sessions[-1].close.astimezone(UTC).hour == 7  # 15:00 CST = 07:00 UTC


def test_lunch_break_is_closed():
    sessions = day_sessions(CN)
    shanghai = timezone(timedelta(hours=8))
    for local_hour, local_minute, expected in [
        (9, 29, False),
        (9, 30, True),
        (11, 29, True),
        (11, 30, False),
        (12, 59, False),
        (13, 0, True),
        (14, 59, True),
        (15, 0, False),
    ]:
        moment = datetime(2026, 3, 9, local_hour, local_minute, tzinfo=shanghai)
        assert (find_active_session(sessions, moment) is not None) is expected, moment


def test_us_daylight_saving_shift_moves_utc_open():
    """同一套本地时段（09:30–16:00），夏令时前后 UTC 开市时刻差一小时。"""
    winter = day_sessions(us_day(US_WINTER_DAY))[0]
    summer = day_sessions(us_day(US_SUMMER_DAY))[0]
    assert winter.open.astimezone(UTC).hour == 14  # 冬令时 EST：09:30 → 14:30 UTC
    assert summer.open.astimezone(UTC).hour == 13  # 夏令时 EDT：09:30 → 13:30 UTC
    assert (summer.open - summer.close) == (winter.open - winter.close)


def test_extended_sessions_are_opt_in():
    """默认不含盘前盘后：盘中轮询量不能被凭空放大（spec §6.1）。"""
    extended = (("pre", "04:00", "09:30"), ("post", "16:00", "20:00"))
    regular = day_sessions(us_day(US_SUMMER_DAY))
    assert [s.name for s in regular] == ["regular"]

    with_extended = day_sessions(
        us_day(US_SUMMER_DAY), include_extended=True, extended=extended
    )
    assert [s.name for s in with_extended] == ["pre", "regular", "post"]
    assert with_extended[0].open < with_extended[1].open


def test_extended_session_never_eats_the_regular_session():
    """配错的盘后时段（08:00 开始）会被裁到常规收盘之后，而不是盖掉正规时段。"""
    sessions = day_sessions(
        us_day(US_SUMMER_DAY),
        include_extended=True,
        extended=(("post", "08:00", "20:00"), ("pre", "04:00", "15:00")),
    )
    regular = next(s for s in sessions if s.name == "regular")
    post = next(s for s in sessions if s.name == "post")
    pre = next(s for s in sessions if s.name == "pre")
    assert post.open == regular.close
    assert pre.close == regular.open


def test_half_day_reports_the_short_session():
    hk_half = CalendarDay(
        market="HK",
        day=date(2026, 12, 24),
        is_trading=True,
        is_half_day=True,
        tz="Asia/Hong_Kong",
        windows=(SessionWindow("regular", "09:30", "12:30"),),
    )
    sessions = day_sessions(hk_half)
    assert sessions[0].duration_min == 180
    assert sessions[0].close.astimezone(UTC).hour == 4  # 12:30 HKT = 04:30 UTC


def test_post_close_run_at_uses_offset():
    moment = post_close_run_at(CN, 30)
    assert moment.tzinfo == UTC
    assert moment == datetime(2026, 3, 9, 7, 0, tzinfo=UTC) + timedelta(minutes=30)


def test_post_close_run_at_needs_a_trading_day():
    with pytest.raises(ValidationError, match="非交易日"):
        day = CalendarDay("A", date(2026, 10, 1), False, False, "Asia/Shanghai", ())
        post_close_run_at(day, 30)


def test_naive_datetime_is_rejected():
    with pytest.raises(ValidationError, match="tz-aware"):
        find_active_session(day_sessions(CN), datetime(2026, 3, 9, 10, 0))


def test_gap_days_lists_both_open_ends():
    """请求 1~10，锚点落在 3~7：缺口是 1、2 与 8、9、10 —— 两端都要算。

    这条算式决定「这个数是不是请求那段行情的数」，所以它在 core，不在命令行里。
    """
    assert gap_days(D(1), D(3), D(7), D(10)) == [D(1), D(2), D(8), D(9), D(10)]


def test_gap_days_is_empty_when_the_anchors_are_the_request():
    assert gap_days(D(1), D(1), D(10), D(10)) == []
