"""数据源健康：跨层共享的值对象与落库端口。

放 core 的原因和 CalendarStore 一样：datasource 要写它、storage 要实现它、
services 要读它，任何一方 import 另一方都是横向依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass(frozen=True)
class SourceRecord:
    """某个数据源最近一次被路由到的结果。"""

    name: str
    markets: tuple[str, ...]
    #: 三态，不是布尔：`None` 表示这个源从未被路由过。
    #: 把「没调用过」显示成 ok 是最坏的误导——港美股只有 yfinance 一个源，
    #: 它被限流时若备源位显示 ok，健康检查就永远指不出真正的单点。
    ok: bool | None = None
    last_error: str | None = None
    last_ok_at: date | None = None
    consecutive_failures: int = 0


class SourceHealthStore(Protocol):
    """源健康的落库出口。

    采集是长进程的事，健康检查是另一条命令（另一个进程）读的事——
    不落库的话 `sams health` 永远只能报告「本进程没失败过」，那是句废话。
    """

    def load(self) -> dict[str, SourceRecord]: ...

    def save(self, record: SourceRecord) -> None: ...


class SourceHealthView(Protocol):
    """健康检查需要的全部能力：读出各源最近结果。AdapterRegistry 实现它。"""

    def health(self) -> list[SourceRecord]: ...
