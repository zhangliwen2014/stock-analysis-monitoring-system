"""运行时：线程起停、`/healthz` 的读数、每日 UTC 03:30 的清理。"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from support import FrozenClock

from sams.core.quotes import Quote
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
    board=None,
    database=None,
    clock=None,
):
    engine = scheduler or FakeScheduler()
    return Runtime(
        settings,
        scheduler=engine,
        schedule_reading=schedule_reading or engine.schedule_data,
        monitor=monitor or FakeMonitor(),
        prune=prune or FakePrune(),
        board=board if board is not None else FakeBoard(),
        database=database,
        clock=clock or FrozenClock(NOW),
    )


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


def test_start_runs_three_threads_and_stop_joins_them(settings):
    scheduler = FakeScheduler()
    monitor = FakeMonitor()
    prune = FakePrune()
    runtime = make_runtime(settings, scheduler=scheduler, monitor=monitor, prune=prune)

    runtime.start()
    assert scheduler.started.wait(2) and monitor.started.wait(2)
    assert prune.called.wait(2), "维护线程启动即清一次"
    assert set(runtime.threads) == {"scheduler", "intraday", "maintenance"}

    runtime.stop()
    assert scheduler.stopped.wait(2) and monitor.stopped.wait(2)
    assert not any(thread.is_alive() for thread in runtime.threads.values())
    runtime.stop()  # 幂等：停机流程被重复调用不该抛


def test_start_twice_is_refused(settings):
    runtime = make_runtime(settings)
    runtime.start()
    try:
        with pytest.raises(RuntimeError, match="只能启动一次"):
            runtime.start()
    finally:
        runtime.stop()
