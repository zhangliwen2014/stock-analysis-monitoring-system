from __future__ import annotations

from datetime import UTC, date, datetime

from support_scheduler import FakeCalendar

from sams.services.scheduler.due import (
    daily_due,
    fx_due,
    monthly_due,
    period_key,
    weekly_due,
)

#: 2024-10-25 是周五；A 股 15:00 收盘，港股 16:00，美股 16:00 纽约时（EDT）。
FRIDAY = date(2024, 10, 25)


def test_each_market_uses_its_own_close(settings):
    cal = FakeCalendar(settings)
    # 07:30 UTC = A 股 15:30（收盘 + 30 已过）、港股 15:30（其收盘 16:30 还没过）、
    # 美股 03:30（还没开）
    now = datetime(2024, 10, 25, 7, 30, tzinfo=UTC)
    assert daily_due(cal, settings, "A", now).as_of == FRIDAY
    assert daily_due(cal, settings, "HK", now).as_of == date(2024, 10, 24)
    assert daily_due(cal, settings, "US", now).as_of == date(2024, 10, 24)


def test_daily_due_waits_for_the_post_close_offset(settings):
    cal = FakeCalendar(settings)
    # A 股 15:00 收盘（07:00 UTC），配置的 30 分钟还没过 ⇒ 最新可跑的是周四
    now = datetime(2024, 10, 25, 7, 0, tzinfo=UTC)
    assert daily_due(cal, settings, "A", now).as_of == date(2024, 10, 24)
    now = datetime(2024, 10, 25, 7, 30, tzinfo=UTC)
    assert daily_due(cal, settings, "A", now).as_of == FRIDAY


def test_weekend_looks_back_to_the_last_open_day(settings):
    cal = FakeCalendar(settings)
    saturday = datetime(2024, 10, 26, 12, 0, tzinfo=UTC)
    due = daily_due(cal, settings, "A", saturday)
    assert due.period_key == "2024-10-25" and due.as_of == FRIDAY


def test_weekly_due_fires_on_the_last_trading_day_of_the_iso_week(settings):
    cal = FakeCalendar(settings)
    thursday = datetime(2024, 10, 24, 23, 0, tzinfo=UTC)
    # 周四收盘后，最近一个「本周最后交易日」还是上周五 10/18（W42）
    assert weekly_due(cal, settings, "A", thursday).period_key == "2024-W42"
    friday = datetime(2024, 10, 25, 23, 0, tzinfo=UTC)
    due = weekly_due(cal, settings, "A", friday)
    assert due.period_key == "2024-W43" and due.as_of == FRIDAY


def test_weekly_due_skips_a_closed_friday(settings):
    cal = FakeCalendar(settings, closed={"A": {FRIDAY}})
    friday = datetime(2024, 10, 25, 23, 0, tzinfo=UTC)
    # 周五休市 ⇒ 本周最后交易日是周四 10/24，也属于 W43
    assert weekly_due(cal, settings, "A", friday).period_key == "2024-W43"
    assert weekly_due(cal, settings, "A", friday).as_of == date(2024, 10, 24)


def test_weekly_does_not_conclude_when_the_next_day_is_unknown(settings):
    """下一交易日不可知 ⇒ 一周都不能宣布结束：这一轮不跑，而不是提前跑或认错周。"""
    cal = FakeCalendar(settings, next_unknown=("A",))
    friday = datetime(2024, 10, 25, 23, 0, tzinfo=UTC)
    assert weekly_due(cal, settings, "A", friday) is None


def test_monthly_due_is_the_daily_due_day_in_that_month(settings):
    cal = FakeCalendar(settings)
    friday = datetime(2024, 10, 25, 23, 0, tzinfo=UTC)
    due = monthly_due(cal, settings, "A", friday)
    assert due.period_key == "2024-10" and due.as_of == FRIDAY


def test_fx_waits_until_every_open_market_has_closed(settings):
    cal = FakeCalendar(settings)
    # 周五 08:00 UTC：A 已收盘，港股的盘后时点（16:30 本地）还没到，美股还在周五盘中
    early = datetime(2024, 10, 25, 8, 0, tzinfo=UTC)
    assert fx_due(cal, settings, early) is None
    # 周五 23:00 UTC：US 周五收盘 + 60 分钟也已过
    late = datetime(2024, 10, 25, 23, 0, tzinfo=UTC)
    assert fx_due(cal, settings, late).as_of == FRIDAY


def test_fx_does_not_wait_for_a_market_that_is_closed(settings):
    cal = FakeCalendar(settings, closed={"A": {FRIDAY}, "HK": {FRIDAY}})
    late = datetime(2024, 10, 25, 23, 0, tzinfo=UTC)
    # 美股周五仍开市且已收盘；A/HK 当天休市不参与等待
    assert fx_due(cal, settings, late).as_of == FRIDAY


def test_period_key_follows_the_job_period():
    assert period_key("daily", FRIDAY) == "2024-10-25"
    assert period_key("weekly", FRIDAY) == "2024-W43"
    assert period_key("monthly", FRIDAY) == "2024-10"
    assert period_key("fx", FRIDAY) == "2024-10-25"
