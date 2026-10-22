"""调度器用例共用的世界：一个不看网络的日历、一个能记下每次调用的 job。

日历只按「工作日开市 + 15:00 收盘」回答，因此到期用例不必等真实日历源，
也不会因为某个市场的真实节假日而改口径。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sams.core.errors import CalendarSourceError
from sams.core.timeutil import CalendarDay, SessionWindow


class FakeCalendar:
    """`CalendarService` 的最小替身：到期计算只用到这三个方法。"""

    def __init__(self, settings, *, closed=None) -> None:
        self.settings = settings
        self.closed = {market: set(days) for market, days in (closed or {}).items()}
        self.windows = (SessionWindow("regular", "09:30", "15:00"),)

    def is_trading_day(self, market: str, day: date) -> bool:
        return day.weekday() < 5 and day not in self.closed.get(market, set())

    def post_close_run_at(
        self, market: str, day: date, offset_min: int, *, extended=()
    ) -> datetime:
        zone = ZoneInfo(self.settings.market_cfg(market).tz)
        close = datetime.combine(day, time(15, 0), tzinfo=zone)
        return (close + timedelta(minutes=offset_min)).astimezone(UTC)

    def next_trading_day(self, market: str, after: date, *, horizon_days: int = 30) -> date:
        cursor = after + timedelta(days=1)
        for _ in range(horizon_days):
            if self.is_trading_day(market, cursor):
                return cursor
            cursor += timedelta(days=1)
        raise CalendarSourceError(f"{market} 在 {after} 之后找不到交易日")


class WeekdayProvider:
    """`CalendarProvider` 替身：一整年铺工作日，给真 `CalendarService` 用。"""

    source = "stub"

    def __init__(self, market: str, tz: str) -> None:
        self.market = market
        self.tz = tz
        self.typical_windows = (SessionWindow("regular", "09:30", "15:00"),)

    def days_for_year(self, year: int) -> list[CalendarDay]:
        cursor = date(year, 1, 1)
        days = []
        while cursor.year == year:
            if cursor.weekday() < 5:
                days.append(
                    CalendarDay(self.market, cursor, True, False, self.tz, self.typical_windows)
                )
            cursor += timedelta(days=1)
        return days
