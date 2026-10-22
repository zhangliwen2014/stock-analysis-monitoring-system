"""`task_runs` 的唯一写入方：一个 job 的一次执行怎么开始、怎么收尾、什么时候准重来。

三条规则只写在这一处（D105），不写进 job 里：每个 job 的失败形状本该一样，
写进 job 的话，某一个忘了看 `period_key`，它就会每 tick 跑一遍。
- 本周期成功过 → 不再跑（`period_key` 是「跑过没有」的唯一依据，D98）；
- 上一轮还在跑且还算新鲜 → 等它（超过 `STALE_RUNNING_H` 的 `running` 是崩溃留下的行，
  读取侧不能把它当活进程，但也不能每个 `running` 都当成死进程）；
- 上一次失败且还在重试间隔内 → 等一等（每 tick 都重拉不是重试，是打源）。
连续失败满 `FAILURES_FOR_ALARM` 个**周期**才发一条 `SystemAlarm`，且一个周期至多告一次——
只在写出它**第一个结论**的那一次（D103）：账本读取按周期分组，因为一个一直失败的周期
每轮重试都多写一行；同一个周期里的重试不是三个周期，否则一次网络抖动很快就能凑满。

`SKIPPED` 不是 `task_runs` 的第四种状态：被这三条拦下的那一轮**不留行**——
「该不该跑」由到期计算与这三条回答，不是一次执行（`task_runs.py` 的文件头）。

异常在这里接住的是 `Exception`，而 `jobs.py` 的每一段循环只接 `SamsError`：这不是矛盾，
是两层各管各的隔离范围。job 那一层的单位是一只票/一对币种/一个用户，接错了就把编程错误
说成「这一只取不到」，而该修的那行代码永远没人去修；这一层的单位是「一个 job 的一晚」，
它冒出来的任何东西都不该带走同一个 tick 里排在后面的那些 job，所以 job 本体的异常
全部在这里接住并记成一行 `failed`（`TypeError: ...`），让健康页与告警去读那一行。
账本写入（`start`/`finish`）不在这个 `try` 里：那一行连自己都写不进去时，这里没有
别的载体能替它说话。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.events import EventBus, SystemAlarm
from sams.services.scheduler.due import Due
from sams.services.scheduler.types import JobContext, JobSpec
from sams.services.scheduler.universe import Universe
from sams.storage.repos.task_runs import FAILED, OK, RUNNING, TaskRun, TaskRunRepo

logger = logging.getLogger("sams.services.scheduler.runner")

#: 失败后多久准重来（D105）。
RETRY_GAP_MIN = 10
#: `running` 超过这个小时数就当它是崩溃留下的行，可以重跑（D105）。
STALE_RUNNING_H = 6
#: 连续多少个**周期**失败才发一条 `SystemAlarm`（D103）。
FAILURES_FOR_ALARM = 3
#: `RunResult` 的第三种结论：这一轮没跑，而且**没有**写 `task_runs`
#: （「该不该跑」由到期计算与上面三条规则回答，不是一次执行）
SKIPPED = "skipped"

#: 数连续失败周期时读几个**周期**：多读一格，第 4 个失败周期才不会被数成第 3 个（D103）。
STREAK_PERIODS = FAILURES_FOR_ALARM + 1


@dataclass(frozen=True)
class RunResult:
    job: str
    market: str | None
    period_key: str
    status: str
    rows_affected: int = 0
    note: str = ""
    error: str = ""
    skip_reason: str = ""
    run_id: str = ""


def failure_streak(runs: Sequence[TaskRun]) -> int:
    """从最近的周期往回数：连续多少个**周期**失败。

    `runs` 必须是每个周期一行（那一行的最近状态）、新到旧——`latest_per_period`
    给的就是这个形状：一个周期的结论就是它的最后一行，成功之后不再被重跑，
    所以这里不需要再按 `period_key` 去重。
    """
    streak = 0
    for run in runs:
        if run.status != FAILED:
            break
        streak += 1
    return streak


@dataclass(frozen=True)
class JobRunner:
    runs: TaskRunRepo
    bus: EventBus
    clock: Clock = SYSTEM_CLOCK

    def execute(self, spec: JobSpec, due: Due, universe: Universe) -> RunResult:
        now = self.clock.now_utc()
        # 查账在 `start` 之前：这一轮自己写的那一行不能成为拦下自己的那一行
        previous = self.runs.latest_for_period(spec.name, spec.market, due.period_key)
        if previous is not None:
            reason = self._skip_reason(previous, due, now)
            if reason:
                return RunResult(
                    job=spec.name,
                    market=spec.market,
                    period_key=due.period_key,
                    status=SKIPPED,
                    skip_reason=reason,
                    run_id=previous.id,
                )
        # 崩溃会留下那行 `running`：它没有结论，所以替它重跑的这一轮就是这个周期的第一个结论
        first_verdict = previous is None or previous.status == RUNNING
        run_id = self.runs.start(spec.name, market=spec.market, period_key=due.period_key)
        context = JobContext(
            period_key=due.period_key,
            as_of=due.as_of,
            market=spec.market,
            universe=universe,
        )
        try:
            outcome = spec.run(context)
        except Exception as exc:
            # 隔离是这一层的职责：一次跑挂不能带走同一个 tick 里排在后面的那些 job
            # （`jobs.py` 那一层只接 `SamsError`，理由写在它的文件头）
            # 行里只留得下 `类型名: 消息`，栈只能进日志，否则编程错误没法读
            logger.exception("job %s 周期 %s 执行异常", spec.name, due.period_key)
            return self._failed(
                spec, due, run_id, f"{type(exc).__name__}: {exc}", first_verdict=first_verdict
            )
        if outcome.ok:
            self.runs.finish(
                run_id,
                status=OK,
                rows_affected=outcome.rows_affected,
                note=outcome.note or None,
            )
            return RunResult(
                job=spec.name,
                market=spec.market,
                period_key=due.period_key,
                status=OK,
                rows_affected=outcome.rows_affected,
                note=outcome.note,
                run_id=run_id,
            )
        return self._failed(
            spec,
            due,
            run_id,
            "；".join(outcome.errors),
            first_verdict=first_verdict,
            rows_affected=outcome.rows_affected,
            note=outcome.note,
        )

    def _skip_reason(self, previous: TaskRun, due: Due, now: datetime) -> str:
        if previous.status == OK:
            return f"周期 {due.period_key} 已经成功"
        if previous.status == RUNNING:
            age = now - previous.started_at
            if age < timedelta(hours=STALE_RUNNING_H):
                return f"上一轮还在跑（已 {int(age.total_seconds() // 60)} 分钟）"
            return ""
        # `failed` 只能由 `finish()` 写出，而它同一句就写了 `finished_at`
        waited = now - previous.finished_at
        if waited < timedelta(minutes=RETRY_GAP_MIN):
            return (
                f"距上次失败 {int(waited.total_seconds() // 60)} 分钟，"
                f"不足 {RETRY_GAP_MIN} 分钟"
            )
        return ""

    def _failed(
        self,
        spec: JobSpec,
        due: Due,
        run_id: str,
        error: str,
        *,
        first_verdict: bool,
        rows_affected: int = 0,
        note: str = "",
    ) -> RunResult:
        self.runs.finish(
            run_id,
            status=FAILED,
            rows_affected=rows_affected,
            error=error,
            note=note or None,
        )
        streak = failure_streak(
            self.runs.latest_per_period(spec.name, spec.market, limit=STREAK_PERIODS)
        )
        # 只在第一个结论上告（D103）：第三个周期重试到第 144 次时连续数还是 3，
        # 没有 `first_verdict` 这一格，告警会从「永不发」翻成「每天发 144 条」
        if first_verdict and streak == FAILURES_FOR_ALARM:
            self.bus.publish(
                SystemAlarm(
                    component=spec.component,
                    message=(
                        f"{spec.title} 连续 {streak} 个周期失败；"
                        f"最近一次（{due.period_key}）：{error}"
                    ),
                )
            )
        return RunResult(
            job=spec.name,
            market=spec.market,
            period_key=due.period_key,
            status=FAILED,
            rows_affected=rows_affected,
            note=note,
            error=error,
            run_id=run_id,
        )
