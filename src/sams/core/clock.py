"""时间来源（端口）。

墙钟只在 `SystemClock` 一处被读取。理由不是洁癖：M3 的历史重放与 M5 的回测要求
「同一个输入跑出同一个输出」，而散落在仓库层与适配器里的 `datetime.now()` 会让重放
写出的时间戳全部变成今天——错误结论看起来像是刚算出来的。

注意边界：这里给的是**真实时刻**，不是**交易日**。某天是否交易日、属于哪个交易日
只能问 `CalendarService`，用 UTC 日期推交易日在美股上恒错一天。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Protocol


class Clock(Protocol):
    def now_utc(self) -> datetime: ...

    def today(self) -> date: ...


class SystemClock:
    name = "system"

    def now_utc(self) -> datetime:
        return datetime.now(UTC)

    def today(self) -> date:
        return self.now_utc().date()


#: 无状态，可以当默认参数用：既有构造点不必全部改动，注入点又始终存在
SYSTEM_CLOCK = SystemClock()
