"""运行时：线程起停、`/healthz` 的读数、每日 UTC 03:30 的清理。"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from support import FrozenClock

from sams.core.errors import SamsError
from sams.core.quotes import Quote
from sams.services import runtime as runtime_module
from sams.services.prune import PruneReport, PruneStep
from sams.services.quotes import MarketBoard
from sams.services.runtime import Runtime
from sams.services.scheduler.report import ScheduleData

NOW = datetime(2026, 10, 23, 2, 0, tzinfo=UTC)


class FakeScheduler:
    def __init__(self, *, beat_age_s=5) -> None:
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.beat_age_s = beat_age_s

    def run_forever(self, *, stop, **_kwargs):
        self.started.set()
        stop.wait(5)
        self.stopped.set()
        return 0

    def schedule_data(self):
        return ScheduleData(
            heartbeat=None, beat_age_s=self.beat_age_s, admins=0, jobs=()
        )


class FakeMonitor:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.stopped = threading.Event()

    def run_forever(self, *, stop, **_kwargs):
        self.started.set()
        stop.wait(5)
        self.stopped.set()
        return 0


class FakeWorker:
    """通知线程的替身：形状照 `_Worker` 协议，记录起止。"""

    #: `stop()` 从这一格算 join 预算：排空窗口之外还要留一次投递（`_Worker.drain_s`）
    drain_s = 10.0

    def __init__(self) -> None:
        self.started = threading.Event()
        self.stopped = threading.Event()

    def run(self, *, stop):
        self.started.set()
        stop.wait(5)
        self.stopped.set()


class FakePrune:
    def __init__(self) -> None:
        self.called = threading.Event()
        self.calls = 0

    def run(self, *, dry_run=False):
        self.calls += 1
        self.called.set()
        return PruneReport(dry_run=dry_run, steps=(PruneStep("quotes_snapshot", 3),))


class FakeBoard:
    def __init__(self, snapshot=None) -> None:
        self._snapshot = snapshot or {}

    def snapshot(self):
        return self._snapshot


class BrokenDb:
    def scalar(self, sql):
        raise RuntimeError("库坏了")


class BrokenScheduler(FakeScheduler):
    def schedule_data(self):
        raise RuntimeError("台账读不到")


def make_runtime(
    settings,
    *,
    scheduler=None,
    schedule_reading=None,
    monitor=None,
    prune=None,
    manual_prune=None,
    worker=None,
    board=None,
    database=None,
    clock=None,
):
    engine = scheduler or FakeScheduler()
    scheduled = prune or FakePrune()
    return Runtime(
        settings,
        scheduler=engine,
        schedule_reading=schedule_reading or engine.schedule_data,
        monitor=monitor or FakeMonitor(),
        prune=scheduled,
        # 测试里的便利：没单给手动那条就复用同一个替身。生产里没有这层便利——
        # 那里的 `manual_prune` 自带一条连接（D113），见 `bootstrap.FreshPruneRunner`
        manual_prune=manual_prune if manual_prune is not None else scheduled,
        worker=worker,
        board=board if board is not None else FakeBoard(),
        database=database,
        clock=clock or FrozenClock(NOW),
    )


def _await_prune(runtime) -> None:
    """等那条手动清理的线程收手：`request_prune` 立刻返回，读数是它之后写的。"""
    deadline = datetime.now(UTC) + timedelta(seconds=5)
    while runtime.last_prune_at is None and datetime.now(UTC) < deadline:
        time.sleep(0.01)


def test_healthz_lists_every_configured_market_before_the_first_poll(settings):
    payload = make_runtime(settings).healthz()
    assert payload["status"] == "ok"
    assert set(payload["intraday"]) == {"A", "HK", "US"}
    assert all(item["last_poll_at"] is None for item in payload["intraday"].values())
    assert payload["uptime_s"] == 0
    assert payload["version"]


def test_healthz_reports_the_newest_quote_age_and_source(settings):
    quote = Quote(
        market="A",
        code="600519",
        ts=NOW - timedelta(seconds=12),
        price=1500.0,
        source="tencent",
    )
    board = FakeBoard(
        {"A": MarketBoard(market="A", quotes={"600519": quote}, last_poll_at=NOW)}
    )
    payload = make_runtime(settings, board=board).healthz()
    assert payload["intraday"]["A"] == {
        "last_poll_at": NOW.isoformat(),
        "age_s": 12,
        "source": "tencent",
        "ok": True,
    }
    assert payload["intraday"]["HK"]["source"] is None


def test_a_failed_poll_degrades_the_status(settings):
    board = FakeBoard({"A": MarketBoard(market="A", last_error="timeout")})
    payload = make_runtime(settings, board=board).healthz()
    assert payload["status"] == "degraded"
    assert payload["intraday"]["A"]["ok"] is False


def test_a_stale_scheduler_or_unreadable_schedule_degrades_the_status(settings):
    stale = make_runtime(settings, scheduler=FakeScheduler(beat_age_s=10_000)).healthz()
    assert stale["status"] == "degraded" and stale["scheduler"]["stale"] is True

    broken = make_runtime(settings, scheduler=BrokenScheduler()).healthz()
    assert broken["status"] == "degraded"
    assert broken["scheduler"]["stale"] is True
    assert "台账读不到" in broken["scheduler"]["error"]


def test_healthz_reads_the_schedule_through_the_injected_probe(settings):
    """`/healthz` 在 HTTP 线程上跑（D113），调度器的库连接属于调度线程。

    组装根注入一条「每次新开连接」的读法；这条测试把「不许直呼 schedule_data」
    钉住——`BrokenScheduler.schedule_data` 一旦被 healthz 碰到就会抛。
    """
    runtime = make_runtime(
        settings,
        scheduler=BrokenScheduler(),
        schedule_reading=lambda: ScheduleData(
            heartbeat=None, beat_age_s=5, admins=0, jobs=()
        ),
    )
    payload = runtime.healthz()
    assert payload["status"] == "ok"
    assert payload["scheduler"] == {"beat_age_s": 5, "stale": False}


def test_an_unreadable_database_is_reported_not_raised(settings):
    payload = make_runtime(settings, database=BrokenDb()).healthz()
    assert payload["status"] == "degraded"
    assert payload["db"] == {"ok": False, "error": "库坏了"}


def test_maintenance_is_at_0330_utc_and_is_due_tomorrow_after_it(settings):
    runtime = make_runtime(settings)
    assert runtime.next_maintenance_at(NOW) == datetime(2026, 10, 23, 3, 30, tzinfo=UTC)
    after = datetime(2026, 10, 23, 4, 0, tzinfo=UTC)
    assert runtime.next_maintenance_at(after) == datetime(2026, 10, 24, 3, 30, tzinfo=UTC)


def test_run_maintenance_passes_dry_run_and_reports_the_total(settings):
    prune = FakePrune()
    runtime = make_runtime(settings, prune=prune)
    report = runtime.run_maintenance(dry_run=True)
    assert prune.calls == 1 and report.dry_run is True and report.total == 3


def test_start_runs_four_threads_and_stop_joins_them(settings):
    scheduler = FakeScheduler()
    monitor = FakeMonitor()
    prune = FakePrune()
    worker = FakeWorker()
    runtime = make_runtime(
        settings, scheduler=scheduler, monitor=monitor, prune=prune, worker=worker
    )

    runtime.start()
    assert scheduler.started.wait(2) and monitor.started.wait(2)
    assert prune.called.wait(2), "维护线程启动即清一次"
    assert set(runtime.threads) == {"scheduler", "intraday", "notify", "maintenance"}

    runtime.stop()
    assert scheduler.stopped.wait(2) and monitor.stopped.wait(2)
    assert worker.started.is_set() and worker.stopped.is_set()
    assert not any(thread.is_alive() for thread in runtime.threads.values())
    runtime.stop()  # 幂等：停机流程被重复调用不该抛


def test_without_a_worker_there_is_no_notify_thread(settings):
    """没接队列的装配（CLI、只跑三条的老用例）不该凭空多一条空转线程。"""
    runtime = make_runtime(settings)
    runtime.start()
    try:
        assert "notify" not in runtime.threads
    finally:
        runtime.stop()


def test_start_twice_is_refused(settings):
    runtime = make_runtime(settings)
    runtime.start()
    try:
        with pytest.raises(RuntimeError, match="只能启动一次"):
            runtime.start()
    finally:
        runtime.stop()


def test_stop_tolerates_a_thread_that_never_started(settings):
    """`start()` 在后面某条线程上失败时，`stop()` 仍要能走完。

    `serve` 的 finally 一定会调 `stop()`；那里再抛一次，原来的启动错误就被盖掉了，
    而 `parts.close()` 也跟着跳过——泄漏的正是启动失败时最该收干净的那些连接。
    """
    runtime = make_runtime(settings)
    runtime._threads = {"scheduler": threading.Thread(target=lambda: None)}

    assert runtime.stop() is True, "一条线程都没起过时，没有什么还在跑"

    assert runtime.stop_event.is_set()


class StubbornMonitor:
    """卡在一次慢调用里的线程：故意不看 stop，要等用例放行才退出。"""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def run_forever(self, *, stop, **_kwargs):
        self.started.set()
        while not self.release.wait(0.01):
            pass
        return 0


def test_stop_reports_whether_every_thread_exited(settings, monkeypatch):
    """返回值是给调用方的信号：False 时共享资源（库连接）不许关。"""
    runtime = make_runtime(settings)
    runtime.start()
    assert runtime.stop() is True, "三条线程都按时退出"

    monkeypatch.setattr(runtime_module, "WORKER_STOP_TIMEOUT_S", 0.05)
    stubborn = StubbornMonitor()
    stuck = make_runtime(settings, monitor=stubborn)
    stuck.start()
    assert stubborn.started.wait(2)
    try:
        assert stuck.stop() is False
    finally:
        stubborn.release.set()


def test_the_notify_budget_is_its_drain_window_plus_one_delivery(settings, monkeypatch):
    """通知线程停产时还要排空（`NotifyWorker.drain_s`），手上那一单也要发完。

    join 预算与排空窗口取等时，一次**正常**的排水会被读成「没退干净」，`serve` 于是
    跳过关闭四条连接。这条用例把排空窗口调短，只钉「预算里含了它」这一件事：
    线程在 base 预算之后、排空+一次投递之内退出，`stop()` 必须报干净。
    """
    release = threading.Event()
    started = threading.Event()

    class Draining:
        drain_s = 0.3

        def run(self, *, stop):
            started.set()
            stop.wait(5)
            release.wait(5)  # 手上那一单：等用例放行

    runtime = make_runtime(settings, worker=Draining())
    runtime.start()
    assert started.wait(2)
    monkeypatch.setattr(runtime_module, "WORKER_STOP_TIMEOUT_S", 0.05)
    monkeypatch.setattr(runtime_module, "ONE_DELIVERY_S", 0.05)
    try:
        threading.Timer(0.2, release.set).start()
        assert runtime.stop() is True, "0.2s 在排空窗口 0.3s 之内：这一趟是正常停机"
    finally:
        release.set()


def test_request_prune_refuses_a_second_run_while_one_is_running(settings):
    release = threading.Event()
    started = threading.Event()

    class SlowPrune:
        def run(self, *, dry_run=False):
            started.set()
            release.wait(timeout=5)
            return PruneReport(dry_run=dry_run, steps=(PruneStep("quotes_snapshot", 3),))

    runtime = make_runtime(settings, prune=SlowPrune())
    assert runtime.request_prune() is True
    assert started.wait(timeout=5) is True
    assert runtime.request_prune() is False
    release.set()
    _await_prune(runtime)
    assert runtime.last_prune == "1 步、合计 3 行"
    assert runtime.last_prune_error is None


def test_a_failing_manual_prune_lands_in_last_prune_error(settings):
    class BadPrune:
        def run(self, *, dry_run=False):
            raise SamsError("库被锁住了")

    runtime = make_runtime(settings, prune=BadPrune())
    assert runtime.request_prune() is True
    _await_prune(runtime)
    assert "库被锁住了" in runtime.last_prune_error

    assert runtime.request_prune() is True, "失败一次后忙标志要回到入口"


def test_a_failing_run_does_not_leave_the_previous_summary(settings):
    """失败那次要清掉上一轮的摘要：健康页把那句与这一轮的时间戳并排印。

    留下旧那句就是「最近一次：1 步、合计 3 行」配上一次失败的时刻——读出来像上一次
    真删了 3 行，而实际上那一轮的删除只做了一半。
    """

    class FirstOkThenBoom:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, *, dry_run=False):
            self.calls += 1
            if self.calls > 1:
                raise SamsError("库被锁住了")
            return PruneReport(dry_run=dry_run, steps=(PruneStep("quotes_snapshot", 3),))

    runtime = make_runtime(settings, manual_prune=FirstOkThenBoom())
    assert runtime.request_prune() is True
    _await_prune(runtime)
    assert runtime.last_prune == "1 步、合计 3 行"

    assert runtime.request_prune() is True
    deadline = datetime.now(UTC) + timedelta(seconds=5)
    while runtime.last_prune is not None and datetime.now(UTC) < deadline:
        time.sleep(0.01)
    assert runtime.last_prune is None
    assert "库被锁住了" in runtime.last_prune_error


def test_the_manual_prune_runs_on_its_own_service_not_the_scheduled_one(settings):
    """`request_prune` 走 `manual_prune`，不退回 `self.prune`（D113）。

    生产里 `self.prune` 连的是维护线程那条连接：手动那一下借用它时，03:30 那一拍
    与这一次会共用 `Database._tx_depth` 那个没有锁的计数——先结束的那边把另一边
    正在写的事务一起 COMMIT 掉，半套删除会被报成一次完整的清理。
    """
    scheduled = FakePrune()
    manual = FakePrune()
    runtime = make_runtime(settings, prune=scheduled, manual_prune=manual)

    assert runtime.request_prune() is True
    _await_prune(runtime)
    assert manual.calls == 1
    assert scheduled.calls == 0, "维护线程的连接一次都不许被手动那一下借走"


def test_request_prune_without_a_manual_connection_says_so(settings):
    """没接线就明说：退回 `self.prune` 正是上面那条事故，不是「还有个备胎」。

    这里直接构造 `Runtime` 而不是走 `make_runtime`：那层便利（没单给手动那条就复用
    `prune`）恰恰是本用例要绕开的东西。
    """
    scheduled = FakePrune()
    engine = FakeScheduler()
    runtime = Runtime(
        settings,
        scheduler=engine,
        schedule_reading=engine.schedule_data,
        monitor=FakeMonitor(),
        prune=scheduled,
        clock=FrozenClock(NOW),
    )

    assert runtime.request_prune() is True
    _await_prune(runtime)
    assert runtime.last_prune_error == runtime_module.NO_MANUAL_PRUNE
    assert runtime.last_prune is None
    assert scheduled.calls == 0


def test_a_prune_thread_that_refuses_to_start_returns_the_busy_flag(settings, monkeypatch):
    """`thread.start()` 抛出来时忙标志要还回去，异常照原样冒出去。

    起不动（系统线程见底）时那条线程永远不会跑，也就没人归还 `_prune_running`：
    此后每一次手动清理都答「正在跑」，而其实一条线程都不存在。
    """
    runtime = make_runtime(settings)
    real_start = threading.Thread.start
    attempts = []

    def flaky_start(thread, *args, **kwargs):
        attempts.append(thread)
        if len(attempts) == 1:
            raise RuntimeError("can't start new thread")
        return real_start(thread, *args, **kwargs)

    monkeypatch.setattr(threading.Thread, "start", flaky_start)
    with pytest.raises(RuntimeError, match="can't start new thread"):
        runtime.request_prune()

    assert runtime.request_prune() is True, "起不来的一次不许把入口永久堵死"
    _await_prune(runtime)
    assert runtime.last_prune == "1 步、合计 3 行"


def test_stop_waits_for_an_in_flight_manual_prune(settings, monkeypatch):
    """`request_prune` 起的线程也在 `stop()` 的 join 名单里。

    `serve` 的 finally 只认那个返回值：False 就不关库连接。少 join 这一条，
    一次手动清理会跑在已经关掉的 DuckDB 连接上——从坐着的人手里抽走桌子，
    而炸出来的是另一条线程里一句看不出原因的错。
    """
    release = threading.Event()
    started = threading.Event()

    class SlowPrune:
        def run(self, *, dry_run=False):
            started.set()
            release.wait(timeout=5)
            return PruneReport(dry_run=dry_run, steps=(PruneStep("quotes_snapshot", 3),))

    runtime = make_runtime(settings, prune=SlowPrune())
    assert runtime.request_prune() is True
    assert started.wait(timeout=5) is True
    monkeypatch.setattr(runtime_module, "WORKER_STOP_TIMEOUT_S", 0.05)
    try:
        assert runtime.stop() is False, "清理还没收手时不许关共享资源"
    finally:
        release.set()
    _await_prune(runtime)
    assert runtime.stop() is True


def test_a_stopping_runtime_refuses_a_new_prune(settings):
    """停机已经开始：这一按若起一条线程，`stop()` 已经走过 join 那一段，
    返回后调用方就去关库——那条清理会握着一条已经被关掉的连接继续写。
    """
    scheduled = FakePrune()
    runtime = make_runtime(settings, prune=scheduled)
    runtime.stop_event.set()

    assert runtime.request_prune() is False
    assert scheduled.calls == 0
    assert runtime.last_prune_at is None, "什么都没跑，就别写下一次「跑过」的时刻"
