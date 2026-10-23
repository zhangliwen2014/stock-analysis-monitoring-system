from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date

import pytest

from sams.core.config import MarketCfg, Settings
from sams.core.errors import CalendarSourceError
from sams.core.timeutil import day_sessions
from sams.datasource.calendar_providers import (
    CN_WINDOWS,
    CnSinaProvider,
    XcalProvider,
    default_providers,
)


def make_settings(**tz) -> Settings:
    """三个市场各一个时区；用例要改哪一个就传哪一个。"""
    zones = {
        "A": "Asia/Shanghai",
        "HK": "Asia/Hong_Kong",
        "US": "America/New_York",
    }
    zones.update(tz)
    return Settings(
        db_path=":memory:",
        secret_key="test",
        report_currency="CNY",
        rate_limit_per_s=1,
        polling_interval_s=10,
        cooldown_min=30,
        markets={
            market: MarketCfg(market, zone, currency, 30, ())
            for market, zone, currency in (
                ("A", zones["A"], "CNY"),
                ("HK", zones["HK"], "HKD"),
                ("US", zones["US"], "USD"),
            )
        },
    )


@pytest.fixture(scope="module")
def providers() -> dict:
    """exchange_calendars 的日历数据随包发布，不需要联网，因此整模块共用一次拉取。"""
    return default_providers(make_settings())


def test_registry_covers_all_three_markets(providers):
    assert set(providers) == {"A", "HK", "US"}
    assert providers["A"].tz == "Asia/Shanghai"
    assert providers["HK"].source == "exchange_calendars:XHKG"
    assert providers["US"].source == "exchange_calendars:XNYS"


def test_the_provider_timezone_comes_from_settings():
    """时区只从 `Settings` 来（§3.2）：provider 自带一个写死的值就是「今天」的第二个主人。
    改配置里的时区，provider 必须跟着改——不然 `due` 与 `is_market_open` 会各说各话。
    """
    providers = default_providers(make_settings(US="America/Chicago"))
    assert providers["US"].tz == "America/Chicago"
    assert providers["HK"].tz == "Asia/Hong_Kong"


def test_only_configured_markets_get_a_provider():
    settings = make_settings()
    only_a = replace(settings, markets={"A": settings.markets["A"]})
    assert set(default_providers(only_a)) == {"A"}


def test_us_daylight_saving_moves_the_utc_open_without_moving_local_hours(providers):
    """本地始终是 09:30，但夏令时前后落到 UTC 差一小时——轮询时刻必须按 UTC 算。"""
    us = providers["US"]
    by_day = {d.day: d for d in us.days_for_year(2026)}
    winter = day_sessions(by_day[date(2026, 3, 6)])[0]
    summer = day_sessions(by_day[date(2026, 3, 9)])[0]

    assert winter.open.strftime("%H:%M") == summer.open.strftime("%H:%M") == "09:30"
    assert winter.open.astimezone(UTC).hour == 14
    assert summer.open.astimezone(UTC).hour == 13


def test_early_close_days_are_flagged_as_half_days(providers):
    hk = {d.day: d for d in providers["HK"].days_for_year(2026)}
    assert hk[date(2026, 12, 24)].is_half_day is True
    assert hk[date(2026, 12, 24)].windows[0].close == "12:00"

    us = {d.day: d for d in providers["US"].days_for_year(2026)}
    assert us[date(2026, 11, 27)].is_half_day is True
    assert us[date(2026, 11, 27)].windows[0].close == "13:00"
    assert date(2026, 11, 26) not in us, "感恩节休市，不该出现在交易日集合里"


def test_holidays_are_absent_not_marked_false(providers):
    us = {d.day for d in providers["US"].days_for_year(2026)}
    assert date(2026, 1, 1) not in us
    assert date(2026, 7, 4) not in us


def test_a_full_year_is_reasonably_populated(providers):
    for market in ("HK", "US"):
        days = providers[market].days_for_year(2026)
        assert 240 <= len(days) <= 262, market
        assert all(d.is_trading and d.tz == providers[market].tz for d in days)


def test_unknown_calendar_fails_as_calendar_error():
    broken = XcalProvider(
        market="XX", xcal_code="NOT_A_CALENDAR", tz="UTC", typical_windows=CN_WINDOWS
    )
    with pytest.raises(CalendarSourceError, match="NOT_A_CALENDAR"):
        broken.days_for_year(2026)


def test_cn_provider_declares_two_sessions_and_its_own_identity():
    provider = CnSinaProvider(tz="Asia/Shanghai")
    assert (provider.market, provider.tz, provider.typical_windows) == (
        "A",
        "Asia/Shanghai",
        CN_WINDOWS,
    )


@pytest.mark.live
def test_live_cn_calendar_knows_the_national_day_break():
    provider = CnSinaProvider(tz="Asia/Shanghai")
    days = {d.day for d in provider.days_for_year(2026)}
    assert date(2026, 10, 1) not in days, "国庆休市必须由官方日历给出"
    assert date(2026, 10, 9) in days
