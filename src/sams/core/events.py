"""进程内事件总线（spec D5）。

这是 A→B 演进的扩展缝：现在进程内同步分发，将来拆服务时只替换本文件的实现
为消息队列 producer/consumer，事件类型定义不动。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, TypeVar

logger = logging.getLogger("sams.events")

E = TypeVar("E", bound="Event")


@dataclass(frozen=True, kw_only=True)
class Event:
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def kind(self) -> str:
        return type(self).__name__


@dataclass(frozen=True, kw_only=True)
class QuoteUpdated(Event):
    market: str
    code: str
    price: float
    pct_chg: float | None = None
    volume: float | None = None
    is_stale: bool = False


@dataclass(frozen=True, kw_only=True)
class IndicatorUpdated(Event):
    market: str
    code: str
    bar_date: date


@dataclass(frozen=True, kw_only=True)
class RuleHit(Event):
    rule_id: str
    user_id: str
    market: str
    code: str
    stage: str
    account_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class AlertTriggered(Event):
    alert_id: str
    rule_id: str
    user_id: str


@dataclass(frozen=True, kw_only=True)
class NotifyFailed(Event):
    alert_id: str
    channel_id: str
    attempt: int
    error: str


@dataclass(frozen=True, kw_only=True)
class SystemAlarm(Event):
    """监控系统监控自己（spec §12）：采集连续失败、日历刷新失败、心跳丢失。"""

    component: str
    message: str


@dataclass(frozen=True, kw_only=True)
class ThesisDrifted(Event):
    thesis_id: str
    user_id: str
    detail: str = ""


@dataclass(frozen=True)
class PublishResult:
    handled: int = 0
    errors: tuple[tuple[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


class EventBus:
    """同步分发；单个 handler 抛异常不得影响其它 handler（spec §12 任务隔离）。"""

    def __init__(self) -> None:
        self._handlers: list[tuple[type[Event], Callable[[Event], None]]] = []

    def subscribe(self, event_type: type[E], handler: Callable[[E], None]) -> None:
        if not issubclass(event_type, Event):
            raise TypeError(f"{event_type} 必须继承 Event")
        self._handlers.append((event_type, handler))

    def publish(self, event: Event) -> PublishResult:
        handled = 0
        errors: list[tuple[str, str]] = []
        for registered, handler in list(self._handlers):
            if not isinstance(event, registered):
                continue
            try:
                handler(event)
                handled += 1
            except Exception as exc:  # noqa: BLE001 — 隔离是这里的职责，不吞栈但继续分发
                name = getattr(handler, "__qualname__", repr(handler))
                errors.append((name, f"{type(exc).__name__}: {exc}"))
                logger.exception("handler %s 处理 %s 失败", name, event.kind)
        return PublishResult(handled=handled, errors=tuple(errors))
