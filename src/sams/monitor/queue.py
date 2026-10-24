"""把「发送」挪出求值线程的那条队列（spec §5.5）。

规则引擎那一侧只做两件快的事：告警落库、offer 进队列。网络重试、免打扰、路由
全在通知线程里跑。队列满 ⇒ abandon：告警留在收件箱可以重发，而阻塞求值线程会让
同一轮里排在后面的市场全等它。

这一层只认识 core 与 storage：monitor 内部的分层红线（`tests/test_layering.py`）。
"""

from __future__ import annotations

import logging
import queue as queue_mod
import time
from collections.abc import Callable
from dataclasses import dataclass

from sams.core.errors import SamsError
from sams.core.events import EventBus, SystemAlarm

logger = logging.getLogger("sams.monitor.queue")

DEFAULT_QUEUE_SIZE = 1000
OFFER_TIMEOUT_S = 1.0
GET_TIMEOUT_S = 0.5
#: 停机后还愿意等多久把剩下的发完。超时没发完的留在队列里：进程退出不该为一杯牛奶等牛
DRAIN_S = 10.0


@dataclass(frozen=True)
class AlertJob:
    """「把库里这一条告警发完」的工作单。只有 id：正文从库里重建，不背内存对象。"""

    alert_id: str

    def run(self, notify) -> None:
        notify.deliver_existing(self.alert_id)


@dataclass(frozen=True)
class CallbackJob:
    """系统告警的搬运单（`SystemAlarmRelay`）：真正的处理在通知线程里跑。"""

    callback: Callable[[object], None]
    event: object

    def run(self, notify) -> None:
        self.callback(self.event)


class AlertQueue:
    """有界队列的一层皮：`offer` 把「满」折成 False，让调用方好决策。"""

    def __init__(self, maxsize: int = DEFAULT_QUEUE_SIZE) -> None:
        self._queue: queue_mod.Queue = queue_mod.Queue(maxsize=maxsize)

    @property
    def maxsize(self) -> int:
        return self._queue.maxsize

    def offer(self, item, *, timeout: float = OFFER_TIMEOUT_S) -> bool:
        try:
            self._queue.put(item, timeout=timeout)
        except queue_mod.Full:
            return False
        return True

    def get(self, *, timeout: float = GET_TIMEOUT_S):
        return self._queue.get(timeout=timeout)

    def empty(self) -> bool:
        return self._queue.empty()

    def qsize(self) -> int:
        return self._queue.qsize()


class QueuedAlertSender:
    """`AlertSender` 的队列版：落库 + offer，两步都不碰网络。

    队列满时不是丢告警：`abandon` 落一行 no_channel，理由里写清「可在收件箱重发」。
    两种结局都返回 alert_id——引擎那一侧的账（去重键、冷却）不因投递方式而不同。
    """

    def __init__(self, notify, queue: AlertQueue) -> None:
        self.notify = notify
        self.queue = queue

    def dispatch(
        self,
        message,
        *,
        user_id: str,
        channel_ids=(),
        rule_id=None,
        account_id=None,
    ) -> str:
        alert = self.notify.record(
            message,
            user_id=user_id,
            channel_ids=list(channel_ids),
            rule_id=rule_id,
            account_id=account_id,
        )
        if self.queue.offer(AlertJob(alert.id)):
            return alert.id
        self.notify.abandon(
            alert.id,
            reason=(
                f"通知队列已满（上限 {self.queue.maxsize}）：发送被丢弃，"
                "告警已落库，可在收件箱重发"
            ),
        )
        return alert.id


class NotifyWorker:
    """通知线程的主循环：取工作单 → 交给 NotifyService。停机后排水 ≤ DRAIN_S。"""

    def __init__(self, notify, queue: AlertQueue, *, drain_s: float = DRAIN_S) -> None:
        self.notify = notify
        self.queue = queue
        self.drain_s = drain_s

    def run(self, *, stop) -> None:
        deadline: float | None = None
        while True:
            if stop.is_set() and deadline is None:
                deadline = time.monotonic() + self.drain_s
            try:
                job = self.queue.get(timeout=GET_TIMEOUT_S)
            except queue_mod.Empty:
                if deadline is not None and (
                    self.queue.empty() or time.monotonic() >= deadline
                ):
                    return
                continue
            try:
                job.run(self.notify)
            except SamsError:
                # 一条发不出去不该把线程带走：后面还有全市场的告警在排队
                logger.exception("通知工作单 %r 失败", job)
            if deadline is not None and (
                self.queue.empty() or time.monotonic() >= deadline
            ):
                return


class SystemAlarmRelay:
    """总线上的系统告警 → 队列。订阅回调只入队：EventBus 的订阅方跑在发布线程里，
    在那里发网络请求会把每个发布者都拖慢。"""

    def __init__(self, queue: AlertQueue, handle: Callable[[object], None]) -> None:
        self._queue = queue
        self._handle = handle

    def attach(self, bus: EventBus) -> None:
        bus.subscribe(SystemAlarm, self._on_alarm)

    def _on_alarm(self, event) -> None:
        self._queue.offer(CallbackJob(self._handle, event))
