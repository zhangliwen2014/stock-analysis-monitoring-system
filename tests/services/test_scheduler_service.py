"""`Scheduler`：注册顺序、市场过滤、`as_of` 覆盖、心跳与常驻循环。

runner 用记录型替身：这一层要钉的是「谁在什么时候被交给 runner」，
执行本身的三条时间规则已经在 `test_scheduler_runner.py` 钉过。
心跳那一半也两头钉：手工 `tick` 一行都不留、常驻循环每一拍打一拍，
而过期那条判据只写在一处（`heartbeat.stale_at`），读数与单实例守卫必须同时翻。
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from support import FrozenClock
from support_scheduler import FakeCalendar

from sams.core.errors import CalendarSourceError, SchedulerBusy, StorageError, ValidationError
from sams.services.scheduler.beatfile import BeatFile, read_beat
from sams.services.scheduler.heartbeat import HEARTBEAT_STALE_S
from sams.services.scheduler.report import STREAK_DISPLAY_PERIODS, collect
from sams.services.scheduler.runner import RunResult
from sams.services.scheduler.service import Scheduler
from sams.services.scheduler.types import JobOutcome, JobSpec
from sams.services.scheduler.universe import Universe
from sams.storage.repos.task_runs import FAILED, OK, HeartbeatRepo, TaskRunRepo
from sams.storage.repos.users import UserRepo

MOMENT = datetime(2024, 10, 25, 8, 30, tzinfo=UTC)
FRIDAY = date(2024, 10, 25)
UNIVERSE = Universe(by_market={}, fx_pairs=())
PASSWORD = "correct-horse-battery"


class RecordingRunner:
    def __init__(self, *, fail_for=()) -> None:
        self.calls: list[tuple[str, str | None, str, date]] = []
        self.fail_for = set(fail_for)

    def execute(self, spec, due, universe):
        self.calls.append((spec.name, spec.market, due.period_key, due.as_of))
        if spec.name in self.fail_for:
            return RunResult(
                job=spec.name,
                market=spec.market,
                period_key=due.period_key,
                status=FAILED,
                error="boom",
            )
        return RunResult(
            job=spec.name,
            market=spec.market,
            period_key=due.period_key,
            status=OK,
        )


class StubUniverse:
    def __init__(self) -> None:
        self.builds = 0

    def build(self) -> Universe:
        self.builds += 1
        return UNIVERSE


class BrokenUniverse:
    """账本读不到：那是 `SamsError`，一轮 tick 的失败不该带走常驻循环。"""

    def build(self) -> Universe:
        raise StorageError("db 挂了")


class ExplodingUniverse:
    """不是 `SamsError`：这一层没有可信的下一步，只能让它炸出去。"""

    def build(self) -> Universe:
        raise RuntimeError("编程错误")


def _noop(ctx):
    return JobOutcome()


def job_spec(name, market, period="daily"):
    return JobSpec(name=name, period=period, market=market, run=_noop)


def make(
    db,
    settings,
    specs,
    runner,
    *,
    clock=None,
    sleep=None,
    universe=None,
    calendar=None,
    beat_path=None,
):
    clock = clock or FrozenClock(MOMENT)
    heartbeats = HeartbeatRepo(db, clock=clock)
    runs = TaskRunRepo(db, clock=clock)
    scheduler = Scheduler(
        settings,
        specs,
        runner=runner,
        runs=runs,
        heartbeats=heartbeats,
        universe=universe or StubUniverse(),
        users=UserRepo(db),
        calendar=calendar or FakeCalendar(settings),
        clock=clock,
        sleep=sleep or (lambda seconds: None),
        beat_path=beat_path,
    )
    return scheduler, heartbeats, runs


def reading(db, specs=(), *, age_seconds=None, clock=None):
    """一份只带心跳读数的 `ScheduleData`：`age_seconds` 是那一拍距今多久。"""
    clock = clock or FrozenClock(MOMENT)
    heartbeats = HeartbeatRepo(db, clock=clock)
    if age_seconds is not None:
        # 写的那一侧用的是自己的时钟：与 `collect` 读的那个相差 `age_seconds`
        wrote_at = clock.now_utc() - timedelta(seconds=age_seconds)
        HeartbeatRepo(db, clock=FrozenClock(wrote_at)).start(note="tick=60s")
    return collect(
        runs=TaskRunRepo(db, clock=clock),
        heartbeats=heartbeats,
        users=UserRepo(db),
        specs=specs,
        clock=clock,
    )


def test_jobs_run_in_registration_order(db, settings):
    runner = RecordingRunner()
    specs = (
        job_spec("bars", "A"),
        job_spec("recheck", "A", "weekly"),
        job_spec("rules", "A"),
        job_spec("fx", None, "fx"),
        job_spec("calendar", "A", "monthly"),
    )
    scheduler, _, _ = make(db, settings, specs, runner)

    report = scheduler.tick(as_of=FRIDAY)

    assert [call[0] for call in runner.calls] == ["bars", "recheck", "rules", "fx", "calendar"]
    # 不传 `now` 时读的是注入的那个时钟，不是墙钟
    assert report.now == MOMENT
    assert len(report.results) == 5
    assert report.errors == () and report.waiting == ()
    assert report.failed == ()


def test_failed_results_are_picked_out_of_the_tick(db, settings):
    runner = RecordingRunner(fail_for={"rules"})
    specs = (job_spec("bars", "A"), job_spec("rules", "A"))
    scheduler, _, _ = make(db, settings, specs, runner)

    report = scheduler.tick(as_of=FRIDAY)

    assert [result.job for result in report.results] == ["bars", "rules"]
    # 只有 `failed` 那一支是 FAILED：跑成的那条不能跟着一起被判成失败
    assert [result.job for result in report.failed] == ["rules"]


def test_as_of_uses_the_period_key_of_its_week(db, settings):
    runner = RecordingRunner()
    scheduler, _, _ = make(db, settings, (job_spec("recheck", "A", "weekly"),), runner)

    scheduler.tick(as_of=FRIDAY, only_job="recheck", only_market="A")

    assert runner.calls == [("recheck", "A", "2024-W43", FRIDAY)]


def test_the_manual_anchor_is_not_the_clock_of_the_tick(db, settings):
    """补跑的那一天是 `as_of`，`now` 只是这一轮的时间戳。
    把锚点换成从时钟读出来的那一天，补跑会静默地写到另一个周期上（D98）。"""
    runner = RecordingRunner()
    scheduler, _, _ = make(db, settings, (job_spec("recheck", "A", "weekly"),), runner)
    december = datetime(2024, 12, 3, 8, 30, tzinfo=UTC)

    report = scheduler.tick(now=december, as_of=FRIDAY, only_job="recheck")

    assert runner.calls == [("recheck", "A", "2024-W43", FRIDAY)]
    assert report.now == december


def test_the_market_filter_narrows_the_selection(db, settings):
    runner = RecordingRunner()
    specs = (job_spec("bars", "A"), job_spec("bars", "HK"), job_spec("calendar", "A", "monthly"))
    scheduler, _, _ = make(db, settings, specs, runner)

    scheduler.tick(as_of=FRIDAY, only_market="HK")

    assert runner.calls == [("bars", "HK", "2024-10-25", FRIDAY)]


def test_an_unmatched_filter_is_refused(db, settings):
    runner = RecordingRunner()
    scheduler, _, _ = make(db, settings, (job_spec("bars", "A"),), runner)

    with pytest.raises(ValidationError, match="没有匹配"):
        scheduler.tick(as_of=FRIDAY, only_job="nope")
    with pytest.raises(ValidationError, match="没有匹配"):
        scheduler.tick(as_of=FRIDAY, only_market="US")


def test_the_refusal_names_the_filter_and_the_registry(db, settings):
    """一句话要能看出两件事：哪一个词打错了（`job=`/`market=`），以及这里有什么。"""
    specs = (job_spec("bars", "A"), job_spec("fx", None, "fx"))
    scheduler, _, _ = make(db, settings, specs, RecordingRunner())

    with pytest.raises(ValidationError, match=r"job=nope"):
        scheduler.tick(as_of=FRIDAY, only_job="nope")
    with pytest.raises(ValidationError, match=r"market=US"):
        scheduler.tick(as_of=FRIDAY, only_market="US")
    # 两个条件都敲上时两个都要说：只说一个会让人去改那个没敲错的地方
    with pytest.raises(ValidationError, match=r"job=bars、market=US"):
        scheduler.tick(as_of=FRIDAY, only_job="bars", only_market="US")
    with pytest.raises(ValidationError, match=r"现有 \['bars', 'fx'\]"):
        scheduler.tick(as_of=FRIDAY, only_job="nope")


def test_an_empty_registry_is_refused_too(db, settings):
    """没登记 job 与「筛选条件敲错了」是两个原因：少了「全部」那一个字，
    前者会被读成后者的一个特例，于是人去查命令，而该查的是这里登记了什么。"""
    scheduler, _, _ = make(db, settings, (), RecordingRunner())

    with pytest.raises(ValidationError, match="没有匹配的 job（全部）"):
        scheduler.tick(as_of=FRIDAY)


def test_a_broken_calendar_does_not_stop_the_next_market(db, settings):
    class BrokenCalendar(FakeCalendar):
        def is_trading_day(self, market, day):
            if market == "A":
                raise CalendarSourceError("日历源挂了")
            return super().is_trading_day(market, day)

    runner = RecordingRunner()
    specs = (job_spec("bars", "A"), job_spec("bars", "HK"))
    scheduler, _, _ = make(db, settings, specs, runner, calendar=BrokenCalendar(settings))

    report = scheduler.tick(now=MOMENT)

    assert len(report.errors) == 1
    assert "bars@A" in report.errors[0] and "日历源挂了" in report.errors[0]
    assert [call[1] for call in runner.calls] == ["HK"]


def test_an_unexpected_calendar_error_is_not_collected_as_a_tick_error(db, settings):
    """这里隔离的只有 `SamsError`：日历实现里的编程错误不能被收成一句「这一轮算不出」，
    那样一种坏会被读成一种等。"""

    class BoomingCalendar(FakeCalendar):
        def is_trading_day(self, market, day):
            raise RuntimeError("日历在读一个不存在的键")

    scheduler, _, _ = make(
        db,
        settings,
        (job_spec("bars", "A"),),
        RecordingRunner(),
        calendar=BoomingCalendar(settings),
    )

    with pytest.raises(RuntimeError, match="不存在的键"):
        scheduler.tick(now=MOMENT)


def test_fx_waits_until_every_market_has_closed(db, settings):
    runner = RecordingRunner()
    scheduler, _, _ = make(db, settings, (job_spec("fx", None, "fx"),), runner)

    early = scheduler.tick(now=datetime(2024, 10, 25, 8, 0, tzinfo=UTC))
    assert early.results == ()
    assert early.waiting == ("fx@全局",)

    late = scheduler.tick(now=datetime(2024, 10, 25, 23, 0, tzinfo=UTC))
    assert late.waiting == ()
    assert runner.calls == [("fx", None, "2024-10-25", FRIDAY)]


def test_the_universe_is_built_once_per_tick(db, settings):
    runner = RecordingRunner()
    universe = StubUniverse()
    specs = (job_spec("bars", "A"), job_spec("rules", "A"))
    scheduler, _, _ = make(db, settings, specs, runner, universe=universe)

    scheduler.tick(as_of=FRIDAY)

    assert universe.builds == 1
    assert len(runner.calls) == 2


def test_a_manual_tick_does_not_leave_a_heartbeat(db, settings):
    runner = RecordingRunner()
    scheduler, heartbeats, _ = make(db, settings, (job_spec("bars", "A"),), runner)

    scheduler.tick(as_of=FRIDAY)

    assert heartbeats.latest() is None


def test_the_beat_file_follows_the_loop_and_disappears_on_exit(db, settings, tmp_path):
    """库被这个循环握着时，外部读者打不开库；库旁那一拍就是它能看见的唯一东西（D111）。

    正常退出要把文件带走：留下一个越来越旧的文件，会把「我停了」说成「我卡住了」。
    """
    path = tmp_path / "sams.duckdb.heartbeat.json"
    scheduler, _, _ = make(
        db,
        settings,
        (job_spec("bars", "A"),),
        RecordingRunner(),
        beat_path=path,
    )
    seen = []
    scheduler.run_forever(max_ticks=1, on_tick=lambda report: seen.append(read_beat(path)))

    assert seen == [BeatFile(pid=os.getpid(), beat_at=MOMENT)]
    assert not path.exists()


def test_a_long_chain_refreshes_the_beat_between_jobs(db, settings, tmp_path):
    """一个 job 做上十分钟时，只在每轮开头打的那一拍会把「还在做活」读成「已经停了」。

    补拍的位置必须在 job 之间：这样最坏年龄是「最长的单个 job」，不再是「整条链」。
    单个 job 自身超过阈值的那一段仍然补不了（这里没有第二个线程），这一点写在
    `heartbeat.py` 里，不假装它被修好了。
    """
    clock = FrozenClock(MOMENT)
    path = tmp_path / "sams.duckdb.heartbeat.json"

    class SlowFirstJob(RecordingRunner):
        def execute(self, spec, due, universe):
            result = super().execute(spec, due, universe)
            if len(self.calls) == 1:
                clock.advance(seconds=HEARTBEAT_STALE_S + 60)
            return result

    runner = SlowFirstJob()
    specs = (job_spec("bars", "A"), job_spec("rules", "A"))
    scheduler, heartbeats, _ = make(
        db, settings, specs, runner, clock=clock, beat_path=path
    )
    seen = {}

    def on_tick(report):
        seen["db"] = heartbeats.latest()
        seen["file"] = read_beat(path)
        seen["now"] = clock.now_utc()

    scheduler.run_forever(max_ticks=1, on_tick=on_tick)

    assert seen["now"] - seen["db"].beat_at == timedelta(0)
    assert seen["file"].beat_at == seen["now"]


def test_a_broken_beat_file_does_not_stop_the_loop(db, settings, tmp_path, caplog):
    """库里的那一拍仍然写得进去，文件写不进去只是「外部读者少一条路」，
    不是「这一轮不该跑」。磁盘满或路径被人改成目录时，调度器要继续做活。
    """
    path = tmp_path / "sams.duckdb.heartbeat.json"
    path.mkdir()
    scheduler, heartbeats, _ = make(
        db, settings, (job_spec("bars", "A"),), RecordingRunner(), beat_path=path
    )

    with caplog.at_level(logging.WARNING):
        ticks = scheduler.run_forever(max_ticks=1)

    assert ticks == 1
    assert heartbeats.latest() is not None
    assert "心跳文件写不进去" in caplog.text


def test_the_beat_goes_out_before_the_selection(db, settings):
    """一个 job 都没选出的那一轮，那一拍也已经打出去了：选 job 失败同样是
    「循环还在转」的一轮。挪到选取之后，读的那一侧就把它与「进程停了」混成一个。"""
    clock = FrozenClock(MOMENT)
    scheduler, heartbeats, _ = make(
        db, settings, (job_spec("bars", "A"),), RecordingRunner(), clock=clock
    )
    scheduler.run_forever(max_ticks=1)
    clock.advance(seconds=30)

    with pytest.raises(ValidationError, match="没有匹配"):
        scheduler.tick(as_of=FRIDAY, only_job="nope")

    assert heartbeats.latest().beat_at == MOMENT + timedelta(seconds=30)


def test_run_forever_beats_on_every_tick(db, settings):
    runner = RecordingRunner()
    clock = FrozenClock(MOMENT)
    reports = []
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds=seconds)

    scheduler, heartbeats, _ = make(
        db, settings, (job_spec("bars", "A"),), runner, clock=clock, sleep=sleep
    )

    ticks = scheduler.run_forever(max_ticks=2, on_tick=reports.append)

    assert ticks == 2
    assert len(reports) == 2
    beat = heartbeats.latest()
    assert beat.note == f"tick={settings.scheduler_tick_s}s"
    # 第二拍是第二轮 tick 打的，不是认领时那一次：时钟只被 `sleep` 推过一格
    assert beat.beat_at == MOMENT + timedelta(seconds=settings.scheduler_tick_s)
    # 等格数比拍数少一个：最后一拍之后不再等
    assert sleeps == [settings.scheduler_tick_s]


def test_zero_rounds_leave_no_tick_and_no_claim(db, settings):
    """`max_ticks=0`（或负数）一轮都不跑，也不认领心跳：留下一行刚打的拍会把下一个常驻循环
    挡在阈值外面，而这一次连活都还没开始。"""
    for max_ticks in (0, -1):
        runner = RecordingRunner()
        scheduler, heartbeats, _ = make(db, settings, (job_spec("bars", "A"),), runner)
        reports = []

        ticks = scheduler.run_forever(max_ticks=max_ticks, on_tick=reports.append)

        assert ticks == 0
        assert reports == []
        assert runner.calls == []
        assert heartbeats.latest() is None


def test_the_dependencies_are_passed_by_name(db, settings):
    """`jobs` 之后六个依赖里有三个同为仓库对象、两个同为注入的替身：写错顺序的两份
    都构造得出对象，症状要到第一次真跑才看得见。所以这一句 `TypeError` 是那道墙。"""
    args = (
        settings,
        (job_spec("bars", "A"),),
        RecordingRunner(),
        TaskRunRepo(db),
        HeartbeatRepo(db),
        StubUniverse(),
        UserRepo(db),
        FakeCalendar(settings),
    )

    with pytest.raises(TypeError, match="positional"):
        Scheduler(*args)


def test_the_same_scheduler_can_start_its_loop_again(db, settings):
    scheduler, heartbeats, _ = make(db, settings, (job_spec("bars", "A"),), RecordingRunner())

    scheduler.run_forever(max_ticks=1)
    claimed = heartbeats.latest().id
    scheduler.run_forever(max_ticks=1)

    # 再次进循环不与自己抢心跳，也不另起一行
    assert heartbeats.latest().id == claimed


def test_a_second_daemon_is_refused(db, settings):
    first, _, _ = make(db, settings, (job_spec("bars", "A"),), RecordingRunner())
    second, _, _ = make(db, settings, (job_spec("bars", "A"),), RecordingRunner())

    first.run_forever(max_ticks=1)

    with pytest.raises(SchedulerBusy, match="心跳"):
        second.run_forever(max_ticks=1)


def test_the_guard_reads_the_last_beat_not_the_row_being_created(db, settings):
    """`started_at` 是那一行被创建的时刻，「还活着」看的是最后一次 `beat_at`。
    读错那一列，一个刚打过拍的循环会被看成六分钟前起、于是被接管。"""
    beat_id = HeartbeatRepo(db, clock=FrozenClock(MOMENT - timedelta(minutes=6))).start(
        note="tick=60s"
    )
    HeartbeatRepo(db, clock=FrozenClock(MOMENT)).beat(beat_id)
    scheduler, _, _ = make(db, settings, (job_spec("bars", "A"),), RecordingRunner())

    with pytest.raises(SchedulerBusy, match="心跳"):
        scheduler.run_forever(max_ticks=1)


def test_a_dead_heartbeat_does_not_block_a_new_daemon(db, settings):
    old_clock = FrozenClock(MOMENT - timedelta(minutes=6))
    old = HeartbeatRepo(db, clock=old_clock).start(note="tick=60s")
    scheduler, heartbeats, _ = make(
        db, settings, (job_spec("bars", "A"),), RecordingRunner()
    )

    scheduler.run_forever(max_ticks=1)

    assert heartbeats.latest().id != old


def test_exactly_at_the_threshold_is_stale_and_open_to_takeover(db, settings):
    data = reading(db, age_seconds=HEARTBEAT_STALE_S)
    assert data.beat_age_s == HEARTBEAT_STALE_S
    assert data.stale is True

    scheduler, _, _ = make(db, settings, (job_spec("bars", "A"),), RecordingRunner())
    assert scheduler.run_forever(max_ticks=1) == 1


def test_one_second_under_the_threshold_is_fresh_and_refuses_takeover(db, settings):
    data = reading(db, age_seconds=HEARTBEAT_STALE_S - 1)
    assert data.stale is False

    scheduler, _, _ = make(db, settings, (job_spec("bars", "A"),), RecordingRunner())
    with pytest.raises(SchedulerBusy, match="心跳"):
        scheduler.run_forever(max_ticks=1)


def test_a_tick_not_shorter_than_the_heartbeat_threshold_is_refused(db, settings):
    """等待间隔不比阈值短，形状就是「一个空转的循环也会把自己读成停了」：
    读数与守卫共用同一个比较，两边一起翻错，所以在构造时就挡住，而不是等第一轮。"""
    for tick_s in (HEARTBEAT_STALE_S - 1, 1):
        make(
            db,
            replace(settings, scheduler_tick_s=tick_s),
            (job_spec("bars", "A"),),
            RecordingRunner(),
        )
    for tick_s in (HEARTBEAT_STALE_S, HEARTBEAT_STALE_S + 1):
        with pytest.raises(ValidationError, match="scheduler_tick_s"):
            make(
                db,
                replace(settings, scheduler_tick_s=tick_s),
                (job_spec("bars", "A"),),
                RecordingRunner(),
            )


def test_a_tick_level_failure_is_reported_and_the_loop_survives(db, settings, caplog):
    reports = []
    clock = FrozenClock(MOMENT)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds=seconds)

    scheduler, heartbeats, _ = make(
        db,
        settings,
        (job_spec("bars", "A"),),
        RecordingRunner(),
        clock=clock,
        sleep=sleep,
        universe=BrokenUniverse(),
    )

    with caplog.at_level(logging.WARNING):
        ticks = scheduler.run_forever(max_ticks=2, on_tick=reports.append)

    assert ticks == 2
    assert all("db 挂了" in report.errors[0] for report in reports)
    # 那一拍在做活之前就打出去了：这一轮没做成事，不等于循环不转了
    assert heartbeats.latest().beat_at == MOMENT + timedelta(seconds=settings.scheduler_tick_s)
    # 每一轮失败都要留下一句：只留 `TickReport` 的话，日志里读不到「循环还在转但在失败」
    logged = [rec for rec in caplog.records if "这一轮 tick 失败" in rec.getMessage()]
    assert len(logged) == 2
    # 栈只能留在这里：这一句把异常吞了，往上再没有人能打印它
    assert "in build" in caplog.text


def test_an_interrupt_leaves_a_line_in_the_log(db, settings, caplog):
    """退出要留下一句：`run_forever` 正常返回，返回值看不出它是被打断的还是跑满了轮数——
    那一行是日志里唯一的区别。"""

    def interrupt(seconds):
        raise KeyboardInterrupt

    scheduler, _, _ = make(
        db,
        settings,
        (job_spec("bars", "A"),),
        RecordingRunner(),
        sleep=interrupt,
    )
    with caplog.at_level(logging.WARNING):
        ticks = scheduler.run_forever()

    assert ticks == 1
    assert "收到中断，调度器退出" in caplog.text


def test_an_unexpected_error_is_not_swallowed_by_the_loop(db, settings):
    scheduler, _, _ = make(
        db,
        settings,
        (job_spec("bars", "A"),),
        RecordingRunner(),
        universe=ExplodingUniverse(),
    )

    with pytest.raises(RuntimeError, match="编程错误"):
        scheduler.run_forever(max_ticks=3)


def test_ctrl_c_stops_the_loop_cleanly(db, settings):
    def interrupt(seconds):
        raise KeyboardInterrupt

    scheduler, _, _ = make(
        db, settings, (job_spec("bars", "A"),), RecordingRunner(), sleep=interrupt
    )
    reports = []

    try:
        ticks = scheduler.run_forever(on_tick=reports.append)
    except KeyboardInterrupt as exc:  # 中断是常驻循环的正常退出方式，不是要往上抛的错误
        pytest.fail(f"中断没有被接住：{exc}")

    assert ticks == 1
    assert len(reports) == 1


def test_schedule_data_counts_admins_and_reads_the_heartbeat(db, settings):
    clock = FrozenClock(MOMENT)
    heartbeats = HeartbeatRepo(db, clock=clock)
    runs = TaskRunRepo(db, clock=clock)
    users = UserRepo(db)
    users.create("admin", PASSWORD, is_admin=True)
    gone = users.create("admin-old", PASSWORD, is_admin=True)
    users.disable(gone.id)
    users.create("alice", PASSWORD)
    beat_id = heartbeats.start(note="tick=60s")
    clock.advance(seconds=120)

    data = collect(
        runs=runs,
        heartbeats=heartbeats,
        users=users,
        specs=(job_spec("bars", "A"),),
        clock=clock,
    )

    assert data.heartbeat.id == beat_id
    assert data.beat_age_s == 120
    # 只认「启用且 is_admin」：被禁用的管理员与不是管理员的普通账号各挡住一半
    assert data.admins == 1
    assert data.stale is False and data.never_started is False
    # 读数带着登记的那个周期：这一行读的是 `spec.period`，不是账本上那一行
    assert data.jobs[0].period == "daily"
    assert data.jobs[0].latest is None and data.jobs[0].streak == 0


def test_schedule_data_turns_stale_after_the_threshold(db, settings):
    data = reading(db, age_seconds=HEARTBEAT_STALE_S + 1)

    assert data.stale is True


def test_schedule_data_without_a_heartbeat_is_not_stale(db):
    data = reading(db)

    assert data.never_started is True
    assert data.stale is False
    assert data.beat_age_s is None


def test_the_reading_ages_the_last_beat_not_the_row_being_created(db):
    """与守卫读同一列：年龄从 `beat_at` 算。读成 `started_at` 时，一个一直在打的循环
    会被显示成「六分钟前起的、之后再没动过」。"""
    beat_id = HeartbeatRepo(db, clock=FrozenClock(MOMENT - timedelta(minutes=6))).start(
        note="tick=60s"
    )
    HeartbeatRepo(db, clock=FrozenClock(MOMENT - timedelta(seconds=30))).beat(beat_id)
    clock = FrozenClock(MOMENT)

    data = collect(
        runs=TaskRunRepo(db, clock=clock),
        heartbeats=HeartbeatRepo(db, clock=clock),
        users=UserRepo(db),
        specs=(),
        clock=clock,
    )

    assert data.beat_age_s == 30
    assert data.stale is False


def test_a_beat_written_ahead_of_the_reading_clock_reads_as_zero(db):
    """写那一侧的时钟跑在读取侧前面（两个各自的 `Clock`、或系统时钟被校正过）时，
    年龄不能是负数——负数打印出来是一个看不出原因的数。"""
    heartbeats = HeartbeatRepo(db, clock=FrozenClock(MOMENT + timedelta(seconds=5)))
    heartbeats.start(note="tick=60s")

    data = collect(
        runs=TaskRunRepo(db, clock=FrozenClock(MOMENT)),
        heartbeats=heartbeats,
        users=UserRepo(db),
        specs=(),
        clock=FrozenClock(MOMENT),
    )

    assert data.beat_age_s == 0
    assert data.stale is False


def test_schedule_data_reports_the_latest_run_and_streak(db):
    clock = FrozenClock(MOMENT)
    runs = TaskRunRepo(db, clock=clock)
    for period in ("2024-10-24", "2024-10-25"):
        run_id = runs.start("bars", market="A", period_key=period)
        runs.finish(run_id, status=FAILED, error="boom")
        clock.advance(minutes=1)

    data = collect(
        runs=runs,
        heartbeats=HeartbeatRepo(db, clock=clock),
        users=UserRepo(db),
        specs=(job_spec("bars", "A"), job_spec("fx", None, "fx")),
        clock=clock,
    )

    assert data.jobs[0].latest.period_key == "2024-10-25"
    assert data.jobs[0].streak == 2
    assert data.jobs[1].latest is None and data.jobs[1].streak == 0


def test_the_streak_counts_periods_not_rows(db):
    """同一个周期重试五次只有一个结论：按**行**数窗口时它会被数成五个失败周期。"""
    clock = FrozenClock(MOMENT)
    runs = TaskRunRepo(db, clock=clock)
    for _ in range(5):
        run_id = runs.start("bars", market="A", period_key="2024-10-25")
        runs.finish(run_id, status=FAILED, error="boom")
        clock.advance(minutes=1)

    data = collect(
        runs=runs,
        heartbeats=HeartbeatRepo(db, clock=clock),
        users=UserRepo(db),
        specs=(job_spec("bars", "A"),),
        clock=clock,
    )

    assert data.jobs[0].latest.period_key == "2024-10-25"
    assert data.jobs[0].streak == 1


def test_the_streak_saturates_at_the_display_cap(db):
    """比 `STREAK_DISPLAY_PERIODS` 更长的连败显示那个上限本身，不显示真实周期数。"""
    clock = FrozenClock(MOMENT)
    runs = TaskRunRepo(db, clock=clock)
    last_written = None
    for index in range(STREAK_DISPLAY_PERIODS + 1):
        day = FRIDAY - timedelta(days=index)
        last_written = day.isoformat()
        run_id = runs.start("bars", market="A", period_key=last_written)
        runs.finish(run_id, status=FAILED, error="boom")
        clock.advance(minutes=1)

    data = collect(
        runs=runs,
        heartbeats=HeartbeatRepo(db, clock=clock),
        users=UserRepo(db),
        specs=(job_spec("bars", "A"),),
        clock=clock,
    )

    assert data.jobs[0].streak == STREAK_DISPLAY_PERIODS
    assert data.jobs[0].latest.period_key == last_written


def test_the_scheduler_reports_through_its_own_clock(db, settings):
    clock = FrozenClock(MOMENT)
    scheduler, heartbeats, _ = make(
        db, settings, (job_spec("bars", "A"),), RecordingRunner(), clock=clock
    )
    heartbeats.start(note="tick=60s")

    data = scheduler.schedule_data()

    assert data.heartbeat is not None
    assert data.beat_age_s == 0
