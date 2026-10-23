"""手动请求队列：只绕过到期计算，排队顺序稳定，停止事件收得住循环。

到期 job 的记录型 runner 与 `test_scheduler_service.py` 同一形状；这里额外钉的是
「请求排在到期 job 之前」「锚点按市场本地日」「id 在同一冻住的时钟下仍唯一」，
以及 `stop` 在 tick 之间、等待之中两种位置都能结束循环。
"""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime

import pytest
from support import FrozenClock
from support_scheduler import FakeCalendar

from sams.core.errors import ValidationError
from sams.services.scheduler.runner import RunResult
from sams.services.scheduler.service import RunRequestQueue, Scheduler
from sams.services.scheduler.types import JobOutcome, JobSpec
from sams.services.scheduler.universe import Universe
from sams.storage.repos.task_runs import HeartbeatRepo, TaskRunRepo
from sams.storage.repos.users import UserRepo

#: A/HK 已是 10-25（04:00），US 还是 10-24（16:00）：手动锚点必须按市场换算
MOMENT = datetime(2024, 10, 24, 20, 0, tzinfo=UTC)
FRIDAY = date(2024, 10, 25)
THURSDAY = date(2024, 10, 24)
UNIVERSE = Universe(by_market={}, fx_pairs=())


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str, date]] = []

    def execute(self, spec, due, universe):
        self.calls.append((spec.name, spec.market, due.period_key, due.as_of))
        return RunResult(
            job=spec.name,
            market=spec.market,
            period_key=due.period_key,
            status="ok",
        )


class StubUniverse:
    def build(self) -> Universe:
        return UNIVERSE


class _Stop:
    """`threading.Event` 的替身：只记下等待，不等真时间。"""

    def __init__(self, *, already: bool = False) -> None:
        self._set = already
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return self._set

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        self._set = True
        return True


def _noop(ctx):
    return JobOutcome()


def job_spec(name, market, period="daily"):
    return JobSpec(name=name, period=period, market=market, run=_noop)


def make(db, settings, specs, runner, *, clock=None, sleep=None):
    clock = clock or FrozenClock(MOMENT)
    return Scheduler(
        settings,
        specs,
        runner=runner,
        runs=TaskRunRepo(db, clock=clock),
        heartbeats=HeartbeatRepo(db, clock=clock),
        universe=StubUniverse(),
        users=UserRepo(db),
        calendar=FakeCalendar(settings),
        clock=clock,
        sleep=sleep or (lambda seconds: None),
    ), HeartbeatRepo(db, clock=clock)


def test_a_request_runs_before_the_due_jobs_and_anchors_on_market_local_day(db, settings):
    runner = RecordingRunner()
    scheduler, _ = make(db, settings, (job_spec("bars", "A"),), runner)

    request = scheduler.request_run("bars", "A")
    report = scheduler.tick()

    # 第一行是手动请求（今天），第二行才是到期计算给的昨天
    assert [call[2:] for call in runner.calls] == [
        ("2024-10-25", FRIDAY),
        ("2024-10-24", THURSDAY),
    ]
    assert report.requests[0].request.id == request.id
    assert [result.period_key for result in report.requests[0].results] == ["2024-10-25"]
    assert report.results[0].period_key == "2024-10-25"


def test_an_explicit_as_of_overrides_the_clock(db, settings):
    runner = RecordingRunner()
    scheduler, _ = make(db, settings, (job_spec("bars", "A"),), runner)

    scheduler.request_run("bars", "A", as_of=date(2024, 10, 18))
    scheduler.tick()

    assert runner.calls[0][2:] == ("2024-10-18", date(2024, 10, 18))


def test_a_market_request_covers_every_job_of_that_market(db, settings):
    runner = RecordingRunner()
    specs = (job_spec("bars", "A"), job_spec("rules", "A"), job_spec("bars", "HK"))
    scheduler, _ = make(db, settings, specs, runner)

    scheduler.request_run(market="A")
    report = scheduler.tick(only_market="HK")

    assert [call[:2] for call in runner.calls] == [
        ("bars", "A"),
        ("rules", "A"),
        ("bars", "HK"),
    ]
    assert [result.job for result in report.requests[0].results] == ["bars", "rules"]


def test_a_typo_is_refused_before_it_reaches_the_queue(db, settings):
    scheduler, _ = make(db, settings, (job_spec("bars", "A"),), RecordingRunner())

    with pytest.raises(ValidationError, match=r"job=nope"):
        scheduler.request_run("nope", "A")
    with pytest.raises(ValidationError, match=r"market=ZZ"):
        scheduler.request_run("bars", "ZZ")

    assert scheduler.pending_requests == 0


def test_request_ids_stay_unique_when_the_clock_does_not_move(db, settings):
    scheduler, _ = make(db, settings, (job_spec("bars", "A"),), RecordingRunner())

    first = scheduler.request_run("bars", "A")
    second = scheduler.request_run("bars", "A")

    assert first.id != second.id
    assert first.requested_at == second.requested_at == MOMENT
    assert [request.id for request in scheduler.requests.drain()] == [first.id, second.id]
    assert scheduler.pending_requests == 0


def test_the_queue_loses_nothing_when_many_threads_enqueue():
    queue = RunRequestQueue()
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        for _ in range(25):
            queue.put(job="bars", market="A", as_of=None, requested_at=MOMENT)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    drained = queue.drain()

    assert len(drained) == 100
    assert len({request.id for request in drained}) == 100


def test_stop_set_before_the_loop_leaves_no_heartbeat(db, settings):
    scheduler, heartbeats = make(db, settings, (job_spec("bars", "A"),), RecordingRunner())

    assert scheduler.run_forever(stop=_Stop(already=True)) == 0
    assert heartbeats.latest() is None


def test_stop_during_a_tick_ends_before_the_next_round(db, settings):
    scheduler, _ = make(db, settings, (job_spec("bars", "A"),), RecordingRunner())
    stop = threading.Event()
    reports = []

    def on_tick(report):
        reports.append(report)
        stop.set()

    ticks = scheduler.run_forever(stop=stop, on_tick=on_tick)

    assert ticks == 1
    assert len(reports) == 1


def test_stop_wakes_the_wait_instead_of_the_injected_sleep(db, settings):
    sleeps = []
    scheduler, _ = make(
        db,
        settings,
        (job_spec("bars", "A"),),
        RecordingRunner(),
        sleep=sleeps.append,
    )
    stop = _Stop()

    ticks = scheduler.run_forever(stop=stop)

    assert ticks == 1
    assert sleeps == []
    assert stop.waits == [settings.scheduler_tick_s]
