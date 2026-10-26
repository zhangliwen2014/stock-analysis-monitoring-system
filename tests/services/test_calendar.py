from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from sams.core.errors import CalendarSourceError
from sams.core.events import EventBus, SystemAlarm
from sams.core.timeutil import CalendarDay, SessionWindow
from sams.services.calendar import CalendarService

W = SessionWindow("regular", "09:30", "16:00")


class FakeStore:
    """日历仓库的替身：只记录调用，好让「补齐了几次」可被断言。"""

    def __init__(self, days=(), covered=()):
        self.rows = {d.day: d for d in days}
        self.covered = set(covered)
        self.saved: list[list[CalendarDay]] = []
        self.gets: list[date] = []

    def save(self, days, *, source):
        self.saved.append(list(days))
        for d in days:
            self.rows[d.day] = d
            self.covered.add(d.day.year)
        return len(days)

    def get(self, market, day):
        self.gets.append(day)
        return self.rows.get(day)

    def year_covered(self, market, year, *, through=None):
        return year in self.covered


class FakeProvider:
    market = "HK"
    source = "fake"
    tz = "Asia/Hong_Kong"
    typical_windows = (W,)

    def __init__(self, days=None, error=None):
        self._days = days if days is not None else []
        self._error = error
        self.calls = 0

    def days_for_year(self, year):
        self.calls += 1
        if self._error:
            raise self._error
        return self._days


def trading_day(d: date) -> CalendarDay:
    return CalendarDay("HK", d, True, False, "Asia/Hong_Kong", (W,))


def service(provider=None, store=None, bus=None) -> CalendarService:
    providers = {"HK": provider or FakeProvider()}
    return CalendarService(store or FakeStore(), providers, bus=bus)


def test_known_status_answers_only_what_the_store_can_back_up():
    """`is_trading_day` 在未知时按「是交易日」降级（多轮一次无害），报告里不能这么猜。

    `sams analysis return` 那句「缺口那几天是周末」要是从降级值来的，它就把
    「开市日没采到」说成了常态——而后者才是需要有人去补采集的那种缺口。
    """
    store = FakeStore(days=[trading_day(date(2026, 3, 9))], covered={2026})
    provider = FakeProvider()
    svc = service(provider, store)
    assert svc.known_status("HK", date(2026, 3, 9)) is True
    assert svc.known_status("HK", date(2026, 3, 10)) is False, "已覆盖 + 无行 = 休市"
    assert svc.known_status("HK", date(2027, 3, 9)) is None, "那年没铺，就是不知道"
    assert provider.calls == 0, "问「我们是否已经知道」不该顺手打一次来源"


def test_requires_at_least_one_provider():
    with pytest.raises(CalendarSourceError, match="未配置"):
        CalendarService(FakeStore(), {})


def test_unknown_market_is_named_in_the_error():
    with pytest.raises(CalendarSourceError, match="US"):
        service().provider("US")


def test_a_missing_calendar_source_is_named_in_words_not_a_list_repr():
    """这句话会原样上屏（告警详情的稍后拒绝）：`sorted()` 交出去的是 Python 的
    list repr——「已配置: ['A', 'HK', 'US']」在页面上长得像出了 bug。"""
    svc = CalendarService(
        FakeStore(), {"US": FakeProvider(), "A": FakeProvider(), "HK": FakeProvider()}
    )
    with pytest.raises(CalendarSourceError) as caught:
        svc.provider("SYS")
    message = str(caught.value)
    assert "可用市场：A、HK、US" in message
    assert "[" not in message and "'" not in message


def test_stored_day_is_read_without_calling_the_provider():
    stored = trading_day(date(2026, 3, 9))
    store = FakeStore(days=[stored])
    provider = FakeProvider()
    svc = service(provider, store)
    assert svc.day("HK", stored.day) == stored
    assert provider.calls == 0


def test_covered_year_without_the_day_means_holiday_not_unknown():
    """年份已覆盖却没这天 = 确定休市，不该再问来源，也不该按交易日轮询。"""
    store = FakeStore(days=[trading_day(date(2026, 3, 9))], covered={2026})
    provider = FakeProvider()
    svc = service(provider, store)
    holiday = svc.day("HK", date(2026, 3, 10))
    assert holiday.is_trading is False and holiday.windows == ()
    assert provider.calls == 0


def test_missing_year_is_filled_in_then_read():
    wanted = trading_day(date(2026, 3, 9))
    store = FakeStore()
    provider = FakeProvider(days=[wanted])
    svc = service(provider, store)
    assert svc.day("HK", wanted.day) == wanted
    assert provider.calls == 1
    assert store.saved and store.saved[0] == [wanted]


def test_empty_provider_response_is_an_error_not_a_silent_holiday():
    svc = service(FakeProvider(days=[]), FakeStore())
    with pytest.raises(CalendarSourceError, match="任何交易日"):
        svc.refresh("HK", 2026)


def test_provider_failure_degrades_to_weekday_assumption_and_alarms():
    """spec §6.3：日历未知时按「是交易日」轮询——漏掉真实行情的代价更大。"""
    bus = EventBus()
    alarms: list[SystemAlarm] = []
    bus.subscribe(SystemAlarm, alarms.append)
    provider = FakeProvider(error=CalendarSourceError("来源挂了"))
    store = FakeStore()
    svc = service(provider, store, bus=bus)

    monday = svc.day("HK", date(2026, 3, 9))
    assert monday.is_trading is True
    assert monday.windows == (W,), "降级日必须给出典型时段，否则盘中轮询无法判定开闭市"

    saturday = svc.day("HK", date(2026, 3, 14))
    assert saturday.is_trading is False and saturday.windows == ()

    assert [a.component for a in alarms] == ["calendar"], "同一市场同一年的降级只报一次警，别刷屏"
    assert "2026" in alarms[0].message


def test_repeated_lookups_hit_the_cache_not_the_store():
    wanted = trading_day(date(2026, 3, 9))
    store = FakeStore()
    provider = FakeProvider(days=[wanted])
    svc = service(provider, store)
    assert svc.day("HK", wanted.day) == wanted
    queries = len(store.gets)
    for _ in range(50):
        svc.day("HK", wanted.day)
    assert len(store.gets) == queries, "盘中每 10 秒问一次，不做缓存就是每 10 秒一趟 SQL"
    assert provider.calls == 1


def test_refresh_invalidates_the_cache():
    store = FakeStore(days=[trading_day(date(2026, 3, 9))])
    svc = service(FakeProvider(days=[]), store)
    assert svc.is_trading_day("HK", date(2026, 3, 9))
    fixed = CalendarDay("HK", date(2026, 3, 9), True, True, "Asia/Hong_Kong", (W,))
    provider = FakeProvider(days=[fixed])
    svc.providers["HK"] = provider
    svc.refresh("HK", 2026)
    assert svc.day("HK", date(2026, 3, 9)).is_half_day is True
    assert provider.calls == 1


def test_is_market_open_uses_provider_timezone():
    svc = service(FakeProvider(days=[trading_day(date(2026, 3, 9))]), FakeStore())
    hk = timezone(timedelta(hours=8))
    inside = datetime(2026, 3, 9, 10, 0, tzinfo=hk)
    outside = datetime(2026, 3, 9, 17, 0, tzinfo=hk)
    assert svc.is_market_open("HK", inside)
    assert not svc.is_market_open("HK", outside)


def test_post_close_run_at_is_utc_and_offset():
    svc = service(FakeProvider(days=[trading_day(date(2026, 3, 9))]), FakeStore())
    moment = svc.post_close_run_at("HK", date(2026, 3, 9), 30)
    assert moment.tzinfo is UTC
    assert moment == datetime(2026, 3, 9, 8, 0, tzinfo=UTC) + timedelta(minutes=30)


def test_next_trading_day_skips_holidays():
    store = FakeStore(days=[trading_day(date(2026, 3, 12))], covered={2026})
    svc = service(FakeProvider(), store)
    assert svc.next_trading_day("HK", date(2026, 3, 9)) == date(2026, 3, 12)
    with pytest.raises(CalendarSourceError, match="找不到交易日"):
        svc.next_trading_day("HK", date(2026, 3, 9), horizon_days=1)


def test_sessions_can_opt_into_extended_hours():
    svc = service(FakeProvider(days=[trading_day(date(2026, 3, 9))]), FakeStore())
    regular = svc.sessions("HK", date(2026, 3, 9))
    with_pre = svc.sessions(
        "HK", date(2026, 3, 9), include_extended=True, extended=(("pre", "04:00", "09:30"),)
    )
    assert [s.name for s in regular] == ["regular"]
    assert [s.name for s in with_pre] == ["pre", "regular"]


def test_a_holiday_is_answered_closed_on_the_first_lookup_not_the_second():
    """补齐之后要重新走「已覆盖 + 无行 = 休市」那条判定。

    漏掉这一步的话同一个周末第一次问回答「交易日」（保守降级）、第二次回答「休市」，
    `sams calendar check` 会先打印交易日再打印休市。
    """
    provider = FakeProvider(days=[trading_day(date(2026, 3, 9))])
    svc = service(provider, FakeStore())
    assert svc.day("HK", date(2026, 3, 10)).is_trading is False
    assert svc.day("HK", date(2026, 3, 10)).is_trading is False


class HalfYearStore(FakeStore):
    """来源只给上半年时，真实仓库不会把这一年算成已覆盖。"""

    def save(self, days, *, source):
        written = super().save(days, source=source)
        self.covered.discard(days[0].day.year)
        return written


def test_a_partial_year_is_asked_once_and_answered_conservatively():
    """未覆盖 → 保守按交易日，但一个年份最多问来源一次。

    `next_trading_day` 会连着问 30 天，每次问都补齐等于打来源 30 次同一件事。
    """
    provider = FakeProvider(days=[trading_day(date(2026, 3, 9))])
    svc = service(provider, HalfYearStore())
    assert svc.day("HK", date(2026, 3, 10)).is_trading is True, "方向必须是宁可空转"
    assert svc.day("HK", date(2026, 3, 11)).is_trading is True
    assert provider.calls == 1


def test_an_explicit_refresh_is_a_new_chance_at_the_provider():
    """显式刷新（`sams calendar refresh`）作废「本进程问过了」的登记。"""
    provider = FakeProvider(days=[trading_day(date(2026, 3, 9))])
    svc = service(provider, HalfYearStore())
    svc.day("HK", date(2026, 3, 10))
    assert provider.calls == 1, "第一个未覆盖的日期问了一次，之后的日期不该再问"
    svc.day("HK", date(2026, 3, 11))
    assert provider.calls == 1
    svc.refresh("HK", 2026)
    svc.day("HK", date(2026, 3, 12))
    # 显式刷新自己打了一次来源，又让下一个日期有权再问一次
    assert provider.calls == 3


def test_a_lowercase_market_names_the_same_calendar():
    """`--market hk` 问的是同一份日历：大小写不是语义，服务边界是第二重归一。

    不归一时的答案是「市场 hk 没有日历来源，已配置: ['HK']」——一句话把一个拼写问题
    说成配置缺失，而缓存与「本问过年份」的登记还会为同一件事长两种键。
    """
    provider = FakeProvider(days=[trading_day(date(2026, 3, 9))])
    store = FakeStore(days=[trading_day(date(2026, 3, 9))], covered={2026})
    svc = service(provider, store)
    assert svc.provider("hk") is provider
    assert svc.day("hk", date(2026, 3, 9)).is_trading is True
    assert svc.known_status("hk", date(2026, 3, 9)) is True
    assert svc.is_trading_day("hk", date(2026, 3, 9)) is True
    # 已覆盖的年份里没这一行 = 确定休市，不该因为换了拼写又去问来源
    assert svc.day("HK", date(2026, 3, 10)).is_trading is False
    assert provider.calls == 0


def test_two_provider_keys_collapsing_to_one_market_are_refused():
    """`{"a": …, "A": …}` 归一后是同一个键：静默留一个等于让另一个市场的日历消失。"""
    with pytest.raises(CalendarSourceError, match="撞车"):
        CalendarService(FakeStore(), {"a": FakeProvider(), "A": FakeProvider()})


def test_a_gap_is_bucketed_by_what_the_calendar_can_back_up():
    """`sams analysis return` 的退出码来自这一堆，所以分堆规则不能长在命令行里。

    「缺口那几天是周末」这句话只有在日历真的铺过那几天时才成立；判不了的那天
    必须留在 `unknown` 里，让退出码说「这个数不是请求那段行情的数」。
    """
    covered = FakeStore(days=[trading_day(date(2026, 3, 9))], covered={2026})
    kinds = service(FakeProvider(), covered).gap(
        "HK", [date(2026, 3, 9), date(2026, 3, 10), date(2026, 3, 11)]
    )
    assert kinds.trading == (date(2026, 3, 9),)
    assert kinds.closed == (date(2026, 3, 10), date(2026, 3, 11))
    assert kinds.total == 3 and kinds.matters is True

    # 日历没铺到的那一年：没有行既可能是休市也可能是没数据，只能承认不知道
    bare = service(FakeProvider(), FakeStore(days=[trading_day(date(2026, 3, 9))])).gap(
        "HK", [date(2026, 3, 10)]
    )
    assert bare.unknown == (date(2026, 3, 10),) and bare.matters is True

    # 全是休市日：请求的那段本来就只有这么几根 K 线，这个数就是那段行情
    harmless = service(FakeProvider(), covered).gap("HK", [date(2026, 3, 10)])
    assert harmless.matters is False


def test_filling_a_gap_never_asks_the_provider():
    """问「缺口要不要紧」不能顺手触发采集：一次读数不该打来源。"""
    provider = FakeProvider(days=[])
    svc = service(provider, FakeStore())
    svc.gap("HK", [date(2026, 3, 10), date(2026, 3, 11)])
    assert provider.calls == 0
