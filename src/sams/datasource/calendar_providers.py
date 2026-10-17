"""日历来源实现（策略 + 注册表：加市场 = 新增 Provider 并在 registry 注册）。

A 股以 akshare 官方日历为权威（调休会临时多放）；港股/美股用 exchange_calendars，
其 UTC 时段自带夏令时与提前收市信息。
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sams.core.errors import CalendarSourceError
from sams.core.timeutil import CalendarDay, SessionWindow

logger = logging.getLogger("sams.datasource.calendar")

CN_WINDOWS = (
    SessionWindow("am", "09:30", "11:30"),
    SessionWindow("pm", "13:00", "15:00"),
)


class CnSinaProvider:
    """A 股交易日历：akshare tool_trade_date_hist_sina。"""

    market = "A"
    source = "akshare:sina"
    tz = "Asia/Shanghai"
    typical_windows = CN_WINDOWS

    def days_for_year(self, year: int) -> list[CalendarDay]:
        try:
            import akshare as ak

            frame = ak.tool_trade_date_hist_sina()
        except Exception as exc:  # noqa: BLE001 — 外部依赖失败要转成本域异常，不能裸抛
            raise CalendarSourceError(f"A 股日历拉取失败: {exc}") from exc
        column = "trade_date"
        if column not in frame.columns:
            raise CalendarSourceError(
                f"A 股日历字段漂移: 缺少 {column}，实际 {list(frame.columns)}"
            )
        days = []
        for value in frame[column].to_list():
            day = (
                value
                if isinstance(value, date)
                else datetime.strptime(str(value), "%Y-%m-%d").date()
            )
            if day.year != year:
                continue
            days.append(
                CalendarDay(
                    market=self.market,
                    day=day,
                    is_trading=True,
                    is_half_day=False,
                    tz=self.tz,
                    windows=self.typical_windows,
                )
            )
        if not days:
            raise CalendarSourceError(f"A 股日历无 {year} 年数据")
        return days


class XcalProvider:
    """港股 / 美股交易日历：exchange_calendars。

    半天日按「该年 session 时长众数」判定，而不是去猜各版本的早收 API——
    众数法对夏令时、午休、早收都成立，因为它只看时长这一件事。
    """

    def __init__(
        self,
        *,
        market: str,
        xcal_code: str,
        tz: str,
        typical_windows: tuple[SessionWindow, ...],
    ):
        self.market = market
        self.xcal_code = xcal_code
        self.source = f"exchange_calendars:{xcal_code}"
        self.tz = tz
        self.typical_windows = typical_windows

    def days_for_year(self, year: int) -> list[CalendarDay]:
        try:
            import exchange_calendars as xc

            calendar = xc.get_calendar(self.xcal_code)
            schedule = calendar.schedule
        except Exception as exc:  # noqa: BLE001
            raise CalendarSourceError(f"{self.xcal_code} 日历拉取失败: {exc}") from exc

        window = schedule.loc[f"{year}-01-01" : f"{year}-12-31"]
        if window.empty:
            raise CalendarSourceError(f"{self.xcal_code} 无 {year} 年数据")

        durations = []
        for _, row in window.iterrows():
            minutes = (row["close"] - row["open"]).total_seconds() / 60
            durations.append(minutes)
        modal = Counter(durations).most_common(1)[0][0]

        days = []
        tzinfo = ZoneInfo(self.tz)
        for index, row in window.iterrows():
            day = index.date() if isinstance(index, datetime) else index
            local_open = row["open"].to_pydatetime().astimezone(tzinfo)
            local_close = row["close"].to_pydatetime().astimezone(tzinfo)
            is_half = (row["close"] - row["open"]).total_seconds() / 60 < modal
            days.append(
                CalendarDay(
                    market=self.market,
                    day=day,
                    is_trading=True,
                    is_half_day=is_half,
                    tz=self.tz,
                    windows=(
                        SessionWindow(
                            "regular",
                            local_open.strftime("%H:%M"),
                            local_close.strftime("%H:%M"),
                        ),
                    ),
                )
            )
        return days


HK_WINDOWS = (SessionWindow("regular", "09:30", "16:00"),)
US_WINDOWS = (SessionWindow("regular", "09:30", "16:00"),)


def default_providers() -> dict[str, object]:
    """市场 → 日历来源的注册表。"""
    return {
        "A": CnSinaProvider(),
        "HK": XcalProvider(
            market="HK", xcal_code="XHKG", tz="Asia/Hong_Kong", typical_windows=HK_WINDOWS
        ),
        "US": XcalProvider(
            market="US", xcal_code="XNYS", tz="America/New_York", typical_windows=US_WINDOWS
        ),
    }
