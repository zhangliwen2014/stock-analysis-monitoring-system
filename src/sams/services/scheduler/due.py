"""「现在该跑哪个周期」——只做日期算术，日历的回答从哪里来由 `CalendarView` 决定。

三个决定落在这里，因为它们都是**一行代码就能写错、而且错了不报错**的地方：

* 到期判断以**市场本地**的交易日与其收盘时点为准（D99）：同一瞬间，北京和纽约
  不是同一个「今天」，锚定日必须按市场换算，收盘是否已过也要按市场的时区与
  偏移去问 `CalendarView`。
* 周期键不是「今天」（D98）：周五的任务可能周六补跑，周任务横跨两天，月任务跨月。
* 汇率没有市场维度，但它的到期日要等**当天开市的市场都收盘**（D108）：
  周五晚上用周五的汇率，不能拿美股周四的收盘价去换算。

`CalendarView` 只声明本模块用到的三个方法；`CalendarService` 恰好都实现了。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from sams.core.config import Settings
from sams.core.errors import CalendarSourceError

logger = logging.getLogger("sams.services.scheduler.due")

DAILY, WEEKLY, MONTHLY, FX = "daily", "weekly", "monthly", "fx"
PERIODS = (DAILY, WEEKLY, MONTHLY, FX)

#: 最近到期日的回看窗口：停机十天之内重启，仍能算出一个到期日；再长的停机，
#: 这里一律答 None——一个月的链一次性跑完会打满所有源，要不要补由人决定
CATCHUP_DAYS = 10
#: 周任务往前找「本周最后一个交易日」的跨度：三个整周足够覆盖长假
WEEK_SCAN_DAYS = 21


class CalendarView(Protocol):
    def is_trading_day(self, market: str, day: date) -> bool: ...

    def post_close_run_at(
        self, market: str, day: date, offset_min: int, *, extended=()
    ) -> datetime: ...

    def next_trading_day(self, market: str, after: date, *, horizon_days: int = 30) -> date: ...


@dataclass(frozen=True)
class Due:
    period_key: str
    as_of: date


def local_today(settings: Settings, market: str, now: datetime) -> date:
    """这个市场此刻的本地日期（D99）：同一瞬间，北京和纽约不是同一个「今天」。"""
    return now.astimezone(ZoneInfo(settings.market_cfg(market).tz)).date()


def _passed(view: CalendarView, settings: Settings, market: str, day: date, now: datetime) -> bool:
    offset = settings.market_cfg(market).post_close_min
    return view.post_close_run_at(market, day, offset) <= now


def daily_due(
    view: CalendarView, settings: Settings, market: str, now: datetime
) -> Due | None:
    """最近一个「已经收盘且过了盘后延迟」的交易日；窗口内没有这样的日子就返回 None。"""
    today = local_today(settings, market, now)
    for back in range(CATCHUP_DAYS + 1):
        day = today - timedelta(days=back)
        if view.is_trading_day(market, day) and _passed(view, settings, market, day, now):
            return Due(period_key(DAILY, day), day)
    return None


def weekly_due(
    view: CalendarView, settings: Settings, market: str, now: datetime
) -> Due | None:
    """最近一个「下一个交易日已进入下一 ISO 周」的交易日（D109）。

    判据是**下一个交易日**而不是「周五」：周五休市时，周四就是本周最后一个交易日。
    """
    today = local_today(settings, market, now)
    for back in range(WEEK_SCAN_DAYS + 1):
        day = today - timedelta(days=back)
        if not (view.is_trading_day(market, day) and _passed(view, settings, market, day, now)):
            continue
        try:
            following = view.next_trading_day(market, day)
        except CalendarSourceError as exc:
            # 找不到下一个交易日就不敢替这一周下「已经结束」的结论：跳过这一天，
            # 继续往前找；一直找不到就答不出到期日，而不是替未结束的一周给出结论
            logger.warning("%s %s 的下一交易日不可知：%s", market, day, exc)
            continue
        if following.isocalendar()[:2] == day.isocalendar()[:2]:
            continue
        return Due(period_key(WEEKLY, day), day)
    return None


def monthly_due(
    view: CalendarView, settings: Settings, market: str, now: datetime
) -> Due | None:
    """日历按月刷新：本月最近一个已过盘后时点的交易日，键收成 `YYYY-MM`。"""
    day = daily_due(view, settings, market, now)
    if day is None:
        return None
    return Due(period_key(MONTHLY, day.as_of), day.as_of)


def fx_due(view: CalendarView, settings: Settings, now: datetime) -> Due | None:
    """当天开市的市场都已收盘的最近一天；还没有任何市场拿到到期日时不跑（D108）。"""
    latest: dict[str, date] = {}
    for market in settings.markets:
        due = daily_due(view, settings, market, now)
        if due is not None:
            latest[market] = due.as_of
    if not latest:
        return None
    newest = max(latest.values())
    for market in settings.markets:
        if view.is_trading_day(market, newest) and latest.get(market) != newest:
            # 这个市场那天开市、但它的收盘还没过：等它
            return None
    return Due(period_key(FX, newest), newest)


def period_key(period: str, as_of: date) -> str:
    """手工指定 `as_of` 时用的周期键；形状必须与到期计算写出来的一致（D98）。"""
    if period == WEEKLY:
        iso = as_of.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if period == MONTHLY:
        return as_of.strftime("%Y-%m")
    return as_of.isoformat()


def due_for(
    period: str,
    view: CalendarView,
    settings: Settings,
    *,
    market: str | None,
    now: datetime,
) -> Due | None:
    """一个 job 的到期日。全局 job 只有汇率一类（D108）。"""
    if period == FX:
        return fx_due(view, settings, now)
    if market is None:
        return None
    if period == WEEKLY:
        return weekly_due(view, settings, market, now)
    if period == MONTHLY:
        return monthly_due(view, settings, market, now)
    if period == DAILY:
        return daily_due(view, settings, market, now)
    raise ValueError(f"未知周期 {period!r}：只认 {list(PERIODS)}")
