"""日历端口：核心层只定义契约与降级语义，具体读写由外层实现。

边界约定（spec D5）：core 不 import 除 core 以外的 sams 模块。
* CalendarProvider —— 外层日历来源（akshare / exchange_calendars）实现
* CalendarStore    —— storage.repos.calendar.CalendarRepo 实现
编排两者的 CalendarService 放在 services 层。
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from sams.core.timeutil import CalendarDay, SessionWindow


class CalendarProvider(Protocol):
    """某个市场的日历来源。加市场 = 新增一个 Provider 实现，不改服务层。"""

    market: str
    source: str
    tz: str
    typical_windows: tuple[SessionWindow, ...]

    def days_for_year(self, year: int) -> list[CalendarDay]:
        """返回该年所有交易日（非交易日不必返回）。"""
        ...


class CalendarStore(Protocol):
    """日历读写契约：库为准，未覆盖的年份由服务层触发补齐。"""

    def save(self, days: list[CalendarDay], *, source: str) -> int: ...

    def get(self, market: str, day: date) -> CalendarDay | None: ...

    def year_covered(self, market: str, year: int, *, through: date | None = None) -> bool:
        """该年是否被完整刷新过。`through` 给「数据只可能到今天」的上界（今年）。"""
        ...

