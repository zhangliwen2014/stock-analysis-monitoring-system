"""队列的三个承诺：满不阻塞、逐单隔离、停机排得干（§5.5）。"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

from sams.core.errors import StorageError
from sams.core.events import EventBus, SystemAlarm
from sams.core.notify import AlertMessage
from sams.monitor.queue import (
    AlertJob,
    AlertQueue,
    CallbackJob,
    NotifyWorker,
    QueuedAlertSender,
    SystemAlarmRelay,
)


class FakeNotify:
    def __init__(self) -> None:
        self.recorded: list[AlertMessage] = []
        # 逐参数记下这一次 record 收到什么：`rule_id` 是引擎的去重与冷却键，
        # `dispatch` 少转一个参数，用例要看得见，不能只看见「返回了个 id」
        self.calls: list[tuple[AlertMessage, str, tuple[str, ...], str | None, str | None]] = []
        self.abandoned: list[tuple[str, str]] = []
        self.delivered: list[str] = []
        self.next_id = 1

    def record(
        self, message, *, user_id, channel_ids=(), rule_id=None, account_id=None,
        is_test=False,
    ):
        alert_id = f"a{self.next_id}"
        self.next_id += 1
        self.recorded.append(message)
        self.calls.append((message, user_id, tuple(channel_ids), rule_id, account_id))
        return SimpleNamespace(id=alert_id)

    def abandon(self, alert_id, *, reason):
        self.abandoned.append((alert_id, reason))

    def deliver_existing(self, alert_id, *, context_provider=None):
        self.delivered.append(alert_id)


def msg():
    return AlertMessage(
        market="A",
        code="601088",
        severity="mid",
        triggered_at=datetime.now(UTC),
        name="中国神华",
        account_name="主账户",
        rule_name="PR 低",
        value_text="PR=0.31",
        threshold_text="PR≤0.4",
        action_note="按计划处置",
        stage="hit",
        body="正文",
    )


def test_offer_reports_full_instead_of_raising():
    q = AlertQueue(maxsize=1)
    assert q.offer(AlertJob("a1")) is True
    assert q.offer(AlertJob("a2"), timeout=0.01) is False
    assert q.qsize() == 1
    assert q.get().alert_id == "a1"


def test_sender_records_then_offers():
    notify = FakeNotify()
    q = AlertQueue()
    sender = QueuedAlertSender(notify, q)
    message = msg()
    alert_id = sender.dispatch(
        message,
        user_id="u1",
        channel_ids=["c1", "c2"],
        rule_id="r1",
        account_id="acct1",
    )
    assert alert_id == "a1"
    assert notify.calls == [
        (message, "u1", ("c1", "c2"), "r1", "acct1")
    ]  # 一个参数都不能在转手时掉：rule_id 掉了，引擎那边的去重键就变了
    assert q.qsize() == 1 and notify.delivered == []  # 记了、递了工作单、没发
    q.get().run(notify)
    assert notify.delivered == ["a1"]


def test_a_full_queue_abandons_instead_of_losing_the_alert():
    notify = FakeNotify()
    q = AlertQueue(maxsize=1)
    q.offer(AlertJob("occupied"))
    sender = QueuedAlertSender(notify, q)
    alert_id = sender.dispatch(msg(), user_id="u1")
    assert alert_id == "a1"  # 队列满了也照样给引擎一个 id：去重键与冷却那一侧的账不因投递方式而不同
    assert len(notify.abandoned) == 1
    assert "队列已满" in notify.abandoned[0][1]
    assert "重发" in notify.abandoned[0][1]


def test_a_full_queue_is_decided_immediately_not_by_blocking_the_evaluator():
    notify = FakeNotify()
    q = AlertQueue(maxsize=1)
    q.offer(AlertJob("occupied"))
    sender = QueuedAlertSender(notify, q)
    started = time.monotonic()
    alert_id = sender.dispatch(msg(), user_id="u1")
    elapsed = time.monotonic() - started
    # 满不满要当场决定：求值线程为一条发不出去的告警等一秒，同一轮里排在后面的市场全等它
    assert elapsed < 0.5
    assert alert_id == "a1"
    assert len(notify.abandoned) == 1


def test_worker_drains_after_stop():
    notify = FakeNotify()
    q = AlertQueue()
    for i in range(3):
        q.offer(AlertJob(f"a{i}"))
    stop = threading.Event()
    stop.set()
    NotifyWorker(notify, q, drain_s=5.0).run(stop=stop)
    assert sorted(notify.delivered) == ["a0", "a1", "a2"]
    assert q.empty()


def test_worker_gives_up_when_the_deadline_passes():
    class SlowNotify(FakeNotify):
        def deliver_existing(self, alert_id, *, context_provider=None):
            time.sleep(0.05)
            super().deliver_existing(alert_id)

    notify = SlowNotify()
    q = AlertQueue()
    for i in range(100):
        q.offer(AlertJob(f"a{i}"))
    stop = threading.Event()
    stop.set()
    NotifyWorker(notify, q, drain_s=0.1).run(stop=stop)
    assert q.qsize() > 0  # 排水到点就走：剩下的留在队列里，进程退出不为一杯牛奶等牛


def test_a_callback_job_runs_its_handle_in_the_worker():
    seen: list[object] = []
    notify = FakeNotify()
    q = AlertQueue()
    q.offer(CallbackJob(seen.append, "alarm"))
    stop = threading.Event()
    stop.set()
    NotifyWorker(notify, q, drain_s=2.0).run(stop=stop)
    assert seen == ["alarm"]


def test_one_bad_job_does_not_stop_the_worker():
    notify = FakeNotify()
    calls: list[str] = []

    def deliver(alert_id, *, context_provider=None):
        calls.append(alert_id)
        if alert_id == "bad":
            raise StorageError("库坏了")

    notify.deliver_existing = deliver
    q = AlertQueue()
    q.offer(AlertJob("bad"))
    q.offer(AlertJob("good"))
    stop = threading.Event()
    stop.set()
    NotifyWorker(notify, q, drain_s=2.0).run(stop=stop)
    assert calls == ["bad", "good"]


def test_an_unexpected_failure_abandons_the_alert_and_keeps_the_loop():
    notify = FakeNotify()
    calls: list[str] = []

    def deliver(alert_id, *, context_provider=None):
        calls.append(alert_id)
        if alert_id == "boom":
            raise RuntimeError("线程不该死")

    notify.deliver_existing = deliver
    q = AlertQueue()
    for alert_id in ("boom", "a1", "a2"):
        q.offer(AlertJob(alert_id))
    stop = threading.Event()
    stop.set()
    NotifyWorker(notify, q, drain_s=2.0).run(stop=stop)
    # 异常类型不在本层的名单里也要逐单隔离：后面排着的是全市场的告警
    assert calls == ["boom", "a1", "a2"]
    assert [item[0] for item in notify.abandoned] == ["boom"]
    assert "线程不该死" in notify.abandoned[0][1]  # 那一行要能回答「为什么没发出去」


def test_an_unexpected_failure_in_a_callback_job_leaves_no_abandon():
    seen: list[object] = []

    def boom(event):
        raise RuntimeError("处理不了")

    notify = FakeNotify()
    q = AlertQueue()
    q.offer(CallbackJob(boom, "alarm"))
    q.offer(CallbackJob(seen.append, "alarm2"))
    stop = threading.Event()
    stop.set()
    NotifyWorker(notify, q, drain_s=2.0).run(stop=stop)
    assert seen == ["alarm2"]
    assert notify.abandoned == []  # 系统告警不是库里的一行：没有可弃的东西


def test_the_worker_keeps_consuming_while_stop_is_clear():
    notify = FakeNotify()
    q = AlertQueue()
    stop = threading.Event()
    worker = NotifyWorker(notify, q, drain_s=0.2)
    thread = threading.Thread(target=worker.run, kwargs={"stop": stop}, daemon=True)
    thread.start()
    try:
        time.sleep(0.3)  # 比 drain_s 还久：没收到 stop 的线程不该自己收工
        q.offer(AlertJob("a1"))
        until = time.monotonic() + 2.0
        while "a1" not in notify.delivered and time.monotonic() < until:
            time.sleep(0.01)
        assert notify.delivered == ["a1"]
        assert thread.is_alive()
    finally:
        stop.set()
        thread.join(timeout=2.0)
    assert not thread.is_alive()  # 设了 stop 就该在 drain_s 之内退出，用例不许挂着


def test_a_dropped_system_alarm_is_at_least_spoken_about(caplog):
    bus = EventBus()
    q = AlertQueue(maxsize=1)
    q.offer(CallbackJob(lambda event: None, "occupied"))
    SystemAlarmRelay(q, lambda event: None).attach(bus)
    with caplog.at_level(logging.WARNING, logger="sams.monitor.queue"):
        bus.publish(SystemAlarm(component="akshare", message="连挂三次", ts=datetime.now(UTC)))
    assert q.qsize() == 1  # 挤不进去就留在原地：这里没有可弃的行，也不能把发布线程拖住
    assert "akshare" in caplog.text  # 静默消失的告警等于没有发生过


def test_system_alarms_land_in_the_queue_as_callback_jobs():
    bus = EventBus()
    q = AlertQueue()
    handle_calls: list[SystemAlarm] = []
    SystemAlarmRelay(q, handle_calls.append).attach(bus)
    bus.publish(SystemAlarm(component="akshare", message="连挂三次", ts=datetime.now(UTC)))
    assert q.qsize() == 1  # 订阅回调只入队：EventBus 的订阅方跑在发布线程里
    job = q.get()
    assert isinstance(job, CallbackJob)
