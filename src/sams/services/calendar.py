"""日历服务：库为准，缺失年份向来源补齐，来源全挂时按保守假设降级。

降级方向的依据（spec §6.3）：日历未知时按「是交易日」轮询。
多轮几次空数据无害，误判休市会漏掉真实行情监控。

依赖全部是注入的端口（CalendarStore / CalendarProvider / EventBus），
本模块因此不知道 DuckDB、也不知道 akshare——它只知道契约。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sams.core.calendar import CalendarProvider, CalendarStore
from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import CalendarSourceError
from sams.core.events import EventBus, SystemAlarm
from sams.core.timeutil import (
    CalendarDay,
    Session,
    day_sessions,
    find_active_session,
)
from sams.core.timeutil import (
    post_close_run_at as _post_close_run_at,
)

logger = logging.getLogger("sams.services.calendar")


class CalendarService:
    def __init__(
        self,
        store: CalendarStore,
        providers: dict[str, CalendarProvider],
        *,
        bus: EventBus | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        if not providers:
            raise CalendarSourceError("未配置任何日历来源")
        self.store = store
        self.providers = providers
        self.bus = bus
        self.clock = clock
        self._refresh_attempts: set[tuple[str, int]] = set()
        # 盘中循环每 10 秒就要问「今天是不是交易日」，不做缓存等于每 10 秒一次 SQL 往返
        self._day_cache: dict[tuple[str, date], CalendarDay] = {}
        self._cache_cap = 4096

    def _cache_put(self, key: tuple[str, date], value: CalendarDay) -> None:
        if len(self._day_cache) >= self._cache_cap:
            self._day_cache.clear()
        self._day_cache[key] = value

    def provider(self, market: str) -> CalendarProvider:
        try:
            return self.providers[market]
        except KeyError:
            raise CalendarSourceError(
                f"市场 {market} 没有日历来源，已配置: {sorted(self.providers)}"
            ) from None

    def refresh(self, market: str, year: int) -> int:
        provider = self.provider(market)
        days = provider.days_for_year(year)
        if not days:
            raise CalendarSourceError(f"{provider.source} 未返回 {market} {year} 年任何交易日")
        written = self.store.save(days, source=provider.source)
        self._invalidate(market, year)
        return written

    def _invalidate(self, market: str, year: int) -> None:
        for key in [k for k in self._day_cache if k[0] == market and k[1].year == year]:
            self._day_cache.pop(key, None)
        # 显式刷新（`sams calendar refresh`）是一次新的好意，本进程的「别再问」登记要作废
        self._refresh_attempts.discard((market, year))

    def day(self, market: str, day: date) -> CalendarDay:
        """取当日日历。库中没有且该年未覆盖 → 补齐 → 再取；仍失败 → 保守降级。"""
        key = (market, day)
        cached = self._day_cache.get(key)
        if cached is not None:
            return cached
        resolved = self._resolve_day(market, day)
        self._cache_put(key, resolved)
        return resolved

    def _resolve_day(self, market: str, day: date) -> CalendarDay:
        stored = self.store.get(market, day)
        if stored is not None:
            return stored
        if self.store.year_covered(market, day.year, through=self._today()):
            # 年份已覆盖却没有这一天 = 确定休市，不该再问来源
            return self._closed_day(market, day)
        if (market, day.year) not in self._refresh_attempts:
            # 一个年份最多问来源一次：next_trading_day 会连着问 30 天，
            # 每次都触发补齐等于对日历来源重复打同一件事
            self._refresh_attempts.add((market, day.year))
            try:
                self.refresh(market, day.year)
            except CalendarSourceError as exc:
                logger.error("日历补齐失败，降级为保守假设: %s", exc)
                self._alarm(f"{market} {day.year} 年日历来源不可用: {exc}")
            else:
                loaded = self.store.get(market, day)
                if loaded is not None:
                    return loaded
                if self.store.year_covered(market, day.year, through=self._today()):
                    # 补齐后必须重新走这条判定：否则同一个周末第一次问回答「交易日」、
                    # 第二次回答「休市」，`sams calendar check` 会先打印交易再打印休市
                    return self._closed_day(market, day)
                logger.warning("%s %d 年补齐后仍未铺满：来源给的是半截日历", market, day.year)
                # refresh 会作废这条登记（那是给显式刷新留的），而「问过了还是半截」必须记住，
                # 否则 next_trading_day 连着问 30 天就会打 30 次同一个来源
                self._refresh_attempts.add((market, day.year))
        return self._conservative_day(market, day)

    def _today(self) -> date:
        return self.clock.today()

    def _closed_day(self, market: str, day: date) -> CalendarDay:
        provider = self.provider(market)
        return CalendarDay(
            market=market,
            day=day,
            is_trading=False,
            is_half_day=False,
            tz=provider.tz,
            windows=(),
        )

    def _conservative_day(self, market: str, day: date) -> CalendarDay:
        provider = self.provider(market)
        weekday_guess = day.weekday() < 5
        return CalendarDay(
            market=market,
            day=day,
            is_trading=weekday_guess,
            is_half_day=False,
            tz=provider.tz,
            windows=provider.typical_windows if weekday_guess else (),
        )

    def _alarm(self, message: str) -> None:
        """日历是监控的地基，它坏了必须让人知道（spec §12）。"""
        if self.bus is None:
            return
        self.bus.publish(SystemAlarm(component="calendar", message=message))

    def is_trading_day(self, market: str, day: date) -> bool:
        return self.day(market, day).is_trading

    def known_status(self, market: str, day: date) -> bool | None:
        """日历**能不能**回答这一天：True 开市、False 休市、None 是「还没铺到」。

        与 `is_trading_day` 的差别是方向的差别。轮询时未知按开市处理是对的——多问一次
        空数据无害；把同一个降级值写进报告就错了，那句「那几天是周末」会把
        「开市日没采到」这种真缺口说成常态。这个函数因此只读库，绝不顺手补来源。
        """
        stored = self.store.get(market, day)
        if stored is not None:
            return stored.is_trading
        if self.store.year_covered(market, day.year, through=self._today()):
            return False
        return None

    def sessions(
        self,
        market: str,
        day: date,
        *,
        include_extended: bool = False,
        extended: tuple[tuple[str, str, str], ...] = (),
    ) -> list[Session]:
        return day_sessions(
            self.day(market, day), include_extended=include_extended, extended=extended
        )

    def is_market_open(
        self,
        market: str,
        when: datetime,
        *,
        include_extended: bool = False,
        extended: tuple[tuple[str, str, str], ...] = (),
    ) -> bool:
        provider = self.provider(market)
        local_day = when.astimezone(ZoneInfo(provider.tz)).date()
        sessions = self.sessions(
            market, local_day, include_extended=include_extended, extended=extended
        )
        return find_active_session(sessions, when) is not None

    def post_close_run_at(
        self,
        market: str,
        day: date,
        offset_min: int,
        *,
        extended: tuple[tuple[str, str, str], ...] = (),
    ) -> datetime:
        return _post_close_run_at(self.day(market, day), offset_min, extended=extended)

    def next_trading_day(self, market: str, after: date, *, horizon_days: int = 30) -> date:
        cursor = after + timedelta(days=1)
        for _ in range(horizon_days):
            if self.is_trading_day(market, cursor):
                return cursor
            cursor += timedelta(days=1)
        raise CalendarSourceError(f"{market} 在 {after} 之后 {horizon_days} 天内找不到交易日")
