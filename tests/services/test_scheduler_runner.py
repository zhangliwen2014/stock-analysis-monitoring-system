"""`JobRunner`：记账、重试间隔、连续失败的告警（D103/D105）。

时钟全部由 `FrozenClock` 推进：这些规则说的是「多久之后」，不是「过一会儿」。
每一枚都钉两头——该等的那一轮没跑、等到了的那一轮真跑了，
所以「把间隔写死成零」与「把间隔写成无穷」都能在这里红掉。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from support import FrozenClock

from sams.core.errors import TransientError
from sams.core.events import EventBus, SystemAlarm
from sams.services.scheduler.due import Due
from sams.services.scheduler.runner import (
    RETRY_GAP_MIN,
    SKIPPED,
    STALE_RUNNING_H,
    JobRunner,
)
from sams.services.scheduler.types import JobOutcome, JobSpec
from sams.services.scheduler.universe import Universe
from sams.storage.repos.task_runs import FAILED, OK, RUNNING, TaskRunRepo

MOMENT = datetime(2024, 10, 25, 8, 30, tzinfo=UTC)
UNIVERSE = Universe(by_market={}, fx_pairs=())
DUE = Due(period_key="2024-10-25", as_of=date(2024, 10, 25))


def harness(db, run, *, market="A"):
    clock = FrozenClock(MOMENT)
    runs = TaskRunRepo(db, clock=clock)
    bus = EventBus()
    alarms: list[SystemAlarm] = []
    bus.subscribe(SystemAlarm, alarms.append)
    runner = JobRunner(runs=runs, bus=bus, clock=clock)
    spec = JobSpec(name="bars", period="daily", market=market, run=run)
    return runner, runs, clock, alarms, spec


def test_success_writes_ok_with_rows_and_note(db):
    seen = []

    def job(ctx):
        seen.append(ctx)
        return JobOutcome(rows_affected=3, note="3 只")

    runner, runs, _, _, spec = harness(db, job)
    result = runner.execute(spec, DUE, UNIVERSE)

    assert result.status == OK
    assert result.rows_affected == 3
    row = runs.latest_for_period("bars", "A", "2024-10-25")
    assert (row.status, row.rows_affected, row.note) == (OK, 3, "3 只")
    # 行上的市场就是 spec 的市场：漏写 market 的话这一行会落进「全局」那本账
    assert row.market == "A"
    assert seen[0].period_key == "2024-10-25"
    assert seen[0].as_of == date(2024, 10, 25)
    assert seen[0].market == "A"


def test_a_successful_period_is_not_rerun(db):
    calls = []

    def job(ctx):
        calls.append(ctx.period_key)
        return JobOutcome()

    runner, runs, _, _, spec = harness(db, job)
    first = runner.execute(spec, DUE, UNIVERSE)
    second = runner.execute(spec, DUE, UNIVERSE)

    assert first.status == OK
    assert second.status == SKIPPED
    assert "已经成功" in second.skip_reason
    assert second.run_id == first.run_id
    assert calls == ["2024-10-25"]
    assert runs.count() == 1  # 被拦下的那一轮不留行


def test_a_blank_note_is_stored_as_null(db):
    """job 没说话与 job 说了一个空串在库里必须是两种答案。

    `TaskRun.note` 是 `str | None`：写 `""` 进去，读的一侧就分不出「没写」与「写了个空」。
    """

    def job(ctx):
        return JobOutcome(rows_affected=2)

    runner, runs, _, _, spec = harness(db, job)
    result = runner.execute(spec, DUE, UNIVERSE)

    assert result.note == ""
    row = runs.latest_for_period("bars", "A", "2024-10-25")
    assert row.note is None
    assert row.rows_affected == 2


def test_a_failure_is_retried_only_after_the_gap(db):
    calls = []

    def job(ctx):
        calls.append(1)
        raise TransientError("akshare", "boom")

    runner, runs, clock, _, spec = harness(db, job)
    first = runner.execute(spec, DUE, UNIVERSE)
    assert first.status == FAILED
    assert "boom" in first.error
    assert runs.latest_for_period("bars", "A", "2024-10-25").status == FAILED

    waiting = runner.execute(spec, DUE, UNIVERSE)
    assert waiting.status == SKIPPED
    assert f"不足 {RETRY_GAP_MIN} 分钟" in waiting.skip_reason
    assert len(calls) == 1

    clock.advance(minutes=RETRY_GAP_MIN)
    again = runner.execute(spec, DUE, UNIVERSE)
    assert again.status == FAILED
    assert len(calls) == 2
    assert runs.count() == 2  # 中间那轮等待同样不留行


def test_a_crashed_running_row_is_replaced_only_after_the_limit(db):
    def job(ctx):
        return JobOutcome()

    runner, runs, clock, _, spec = harness(db, job)
    runs.start("bars", market="A", period_key="2024-10-25")

    clock.advance(hours=STALE_RUNNING_H - 1)
    blocked = runner.execute(spec, DUE, UNIVERSE)
    assert blocked.status == SKIPPED
    assert "还在跑" in blocked.skip_reason

    clock.advance(hours=1)
    fresh = runner.execute(spec, DUE, UNIVERSE)
    assert fresh.status == OK
    assert runs.count() == 2
    # 崩溃那一行原样留着：改写它等于抹掉「那一晚死过一次」的证据
    assert [row.status for row in runs.recent("bars", "A", limit=5)] == [OK, RUNNING]


def test_the_third_failed_period_alarms_once(db):
    def job(ctx):
        raise TransientError("akshare", "boom")

    runner, _, _, alarms, spec = harness(db, job)
    for period in ("2024-10-24", "2024-10-25", "2024-10-26"):
        result = runner.execute(
            spec, Due(period_key=period, as_of=date.fromisoformat(period)), UNIVERSE
        )
        assert result.status == FAILED

    assert len(alarms) == 1
    assert alarms[0].component == "job.bars.A"
    assert "连续 3 个周期失败" in alarms[0].message
    assert "boom" in alarms[0].message

    runner.execute(spec, Due(period_key="2024-10-27", as_of=date(2024, 10, 27)), UNIVERSE)
    assert len(alarms) == 1  # 第 4 个失败周期不再加一条（D103）


def test_retries_inside_one_period_do_not_count_as_periods(db):
    def job(ctx):
        raise TransientError("akshare", "boom")

    runner, _, clock, alarms, spec = harness(db, job)
    for _ in range(5):
        runner.execute(spec, DUE, UNIVERSE)
        clock.advance(minutes=RETRY_GAP_MIN)
    assert alarms == []  # 同一个周期的五次重试只算一个失败周期

    runner.execute(spec, Due(period_key="2024-10-26", as_of=date(2024, 10, 26)), UNIVERSE)
    assert alarms == []  # 两个周期

    runner.execute(spec, Due(period_key="2024-10-27", as_of=date(2024, 10, 27)), UNIVERSE)
    assert len(alarms) == 1  # 三个周期


def test_outcome_errors_mark_failed_and_keep_the_note(db):
    def job(ctx):
        return JobOutcome(rows_affected=1, note="1/2 只日线", errors=("600519: boom",))

    runner, runs, _, _, spec = harness(db, job)
    result = runner.execute(spec, DUE, UNIVERSE)

    assert result.status == FAILED
    assert "600519: boom" in result.error
    row = runs.latest_for_period("bars", "A", "2024-10-25")
    assert (row.status, row.rows_affected, row.note) == (FAILED, 1, "1/2 只日线")


def test_a_programming_error_is_recorded_not_raised(db):
    def job(ctx):
        raise TypeError("bad code")

    runner, runs, _, _, spec = harness(db, job)
    result = runner.execute(spec, DUE, UNIVERSE)

    assert result.status == FAILED
    assert "TypeError" in result.error
    assert runs.latest_for_period("bars", "A", "2024-10-25").status == FAILED


def test_global_jobs_and_market_jobs_keep_separate_ledgers(db):
    def job(ctx):
        return JobOutcome()

    runner, runs, _, _, spec = harness(db, job, market=None)
    runner.execute(spec, DUE, UNIVERSE)
    # `job_name` 相同、market 不同：八小时后的 A 股那一行不是「全局那条」的替身
    assert runs.latest_for_period("bars", None, DUE.period_key) is not None
    assert runs.latest_for_period("bars", "A", DUE.period_key) is None


def test_a_global_row_does_not_stand_in_for_the_market_row(db):
    """上面那一枚只钉了「全局看不见 A 股那一行」，这一枚钉反方向。

    查账时漏掉 `market` 的话，A 股那一晚会被全局那条的成功行说成「已经成功」，
    而一个市场的数据都不会拉——症状与「今天不是交易日」一模一样。
    """
    calls = []

    def job(ctx):
        calls.append(ctx.market)
        return JobOutcome()

    runner, _, _, _, global_spec = harness(db, job, market=None)
    runner.execute(global_spec, DUE, UNIVERSE)
    market_spec = JobSpec(name="bars", period="daily", market="A", run=job)

    assert runner.execute(market_spec, DUE, UNIVERSE).status == OK
    assert calls == [None, "A"]


def test_the_running_row_written_by_this_round_is_not_its_own_block(db):
    """查账读的是这一轮之前的行：自己刚写的那一行不能把自己拦下。

    把 `latest_for_period` 挪到 `start` 之后，每一轮都会得到「上一轮还在跑（已 0 分钟）」，
    而 job 一次也不会被执行——注册表、心跳、退出码全都正常。
    """

    def job(ctx):
        return JobOutcome()

    runner, runs, _, _, spec = harness(db, job)
    assert runner.execute(spec, DUE, UNIVERSE).status == OK
    assert runs.latest_for_period("bars", "A", DUE.period_key).status == OK
