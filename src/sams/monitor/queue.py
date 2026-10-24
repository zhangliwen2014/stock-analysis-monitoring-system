"""把「发送」挪出求值线程的那条队列（spec §5.5）。

规则引擎那一侧只做两件快的事：告警落库、offer 进队列。网络重试、免打扰、路由
全在通知线程里跑。队列满 ⇒ abandon：告警留在收件箱可以重发，而阻塞求值线程会让
同一轮里排在后面的市场全等它。通知线程那一侧也一样逐单隔离：一条坏单（包括异常
类型不在名单里的那种）只让它自己那一行写下为什么没发出去，不把线程和后面的队列带走。

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

    def offer(self, item, *, timeout: float = 0.0) -> bool:
        # 默认不等：两个调用方（`QueuedAlertSender.dispatch` 与 `SystemAlarmRelay`）都跑在
        # 别人也要用这条线程的场合，满了就当场 False 让调用方决定怎么交代，等 1 秒等于把
        # 求值线程与发布线程各拖住 1 秒，而那条 docstring 里答应的是「满 ⇒ abandon」
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
        is_test=False,
    ) -> str:
        # `is_test` 只是把端口那一格接住：引擎只会按 False 走，而这里少收一个参数
        # 就等于端口那张参数表骗人（`core/notify.py` 的 `AlertSender` 说了要逐字对齐）
        alert = self.notify.record(
            message,
            user_id=user_id,
            channel_ids=list(channel_ids),
            rule_id=rule_id,
            account_id=account_id,
            is_test=is_test,
        )
        # 满了当场判负（`offer` 的默认超时就是 0）：求值线程多等一秒，同一轮里排在后面的
        # 市场全等它，而这一秒换不来任何东西——队列不会因为等满 1 秒就腾出位置
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
    """通知线程的主循环：取工作单 → 交给 NotifyService。逐单隔离，没收到 stop 就一直消费；
    停机后排水 ≤ DRAIN_S。"""

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
            except Exception as exc:  # noqa: BLE001 — 逐单隔离是这里的职责，异常类型不在名单里也一样
                logger.exception("通知工作单 %r 出了预期外的错", job)
                self._explain(job, exc)
            if deadline is not None and (
                self.queue.empty() or time.monotonic() >= deadline
            ):
                return

    def _explain(self, job, exc: Exception) -> None:
        """把预期外的失败写成一行弃单：这一层的不变式是每条拒绝都落库（`router` 模块开头），
        否则用户那边只剩「我那天为什么没收到」，而栈追踪早就随终端一起关了。

        `CallbackJob` 直接返回：系统告警从来不是收件箱里的一行，没有可弃的东西。
        """
        if not isinstance(job, AlertJob):
            return
        try:
            self.notify.abandon(
                job.alert_id,
                reason=(
                    f"通知线程处理这条告警时出了预期外的错："
                    f"{type(exc).__name__}: {exc}（告警已落库，可在收件箱重发）"
                ),
            )
        except Exception:  # noqa: BLE001 — 弃单本身坏了也不能停在同一个坑里
            logger.exception("给告警 %s 落弃单失败", job.alert_id)


class SystemAlarmRelay:
    """总线上的系统告警 → 队列。订阅回调只入队：EventBus 的订阅方跑在发布线程里，
    在那里发网络请求会把每个发布者都拖慢。"""

    def __init__(self, queue: AlertQueue, handle: Callable[[object], None]) -> None:
        self._queue = queue
        self._handle = handle

    def attach(self, bus: EventBus) -> None:
        bus.subscribe(SystemAlarm, self._on_alarm)

    def _on_alarm(self, event) -> None:
        if not self._queue.offer(CallbackJob(self._handle, event)):
            # 队列满时只能说一句：系统告警不是库里的一行，没有可弃的东西，
            # 而一句也不说就等于让故障静默消失（`router` 那条不变式在这里够不着）
            logger.warning("通知队列已满，系统告警（组件 %s）被丢弃", event.component)
