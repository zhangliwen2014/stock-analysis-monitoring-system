"""`Scheduler`：tick 的顺序、心跳与常驻循环。

一轮 tick 做四件事，顺序就是代码里的顺序：

1. 认领过心跳就打一拍（手工 `tick` 没认领过，什么也不留）；
2. 按 `only_job`/`only_market` 选出这一轮要跑的 job，一个都不匹配就拒绝；
3. 构建这一轮的对象全集——一次 tick 只构建一次，交给每个 job 的是同一份；
4. 按注册顺序逐个 job 判断到期并交给 `JobRunner`，把结果原样交回。

打拍放在做活之前：那一拍证明的只是「循环还在转」，不是「这一轮做完了事」。
挪到收尾之后，一轮做得比阈值久的进程会在做完之前就被读成过期，而守卫只看那一拍的
年龄——它会把还在做活的进程判给第二个常驻进程。

单个 job 的异常在 runner 里已经落账；这里只隔离**到期计算**的异常：
日历源坏了不能让排在后面的市场也不跑。

打印与退出码都不在这里：谁造这个循环、结果怎么给人看，由装配与入口那一侧决定。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.config import Settings
from sams.core.errors import SamsError, SchedulerBusy, ValidationError
from sams.services.scheduler.due import CalendarView, Due, due_for, period_key
from sams.services.scheduler.report import ScheduleData, collect, stale_at
from sams.services.scheduler.runner import JobRunner, RunResult
from sams.services.scheduler.types import JobSpec
from sams.services.scheduler.universe import UniverseBuilder
from sams.storage.repos.task_runs import FAILED, HeartbeatRepo, TaskRunRepo
from sams.storage.repos.users import UserRepo

logger = logging.getLogger("sams.services.scheduler.service")


@dataclass(frozen=True)
class TickReport:
    now: datetime
    results: tuple[RunResult, ...] = ()
    waiting: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def failed(self) -> tuple[RunResult, ...]:
        return tuple(result for result in self.results if result.status == FAILED)


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        jobs: Sequence[JobSpec],
        runner: JobRunner,
        runs: TaskRunRepo,
        heartbeats: HeartbeatRepo,
        universe: UniverseBuilder,
        users: UserRepo,
        calendar: CalendarView,
        *,
        clock: Clock = SYSTEM_CLOCK,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.jobs = tuple(jobs)
        self.runner = runner
        self.runs = runs
        self.heartbeats = heartbeats
        self.universe = universe
        self.users = users
        self.calendar = calendar
        self.clock = clock
        self.sleep = sleep
        self._beat_id: str | None = None

    def tick(
        self,
        now: datetime | None = None,
        *,
        as_of: date | None = None,
        only_job: str | None = None,
        only_market: str | None = None,
    ) -> TickReport:
        moment = now if now is not None else self.clock.now_utc()
        if self._beat_id is not None:
            self.heartbeats.beat(self._beat_id)
        selected = self._selected(only_job, only_market)
        universe = self.universe.build()
        results: list[RunResult] = []
        waiting: list[str] = []
        errors: list[str] = []
        for spec in selected:
            if as_of is not None:
                # 手工补跑：指定的那一天就是这一轮的锚点，周期键仍按 job 自己的周期算，
                # 于是它与到期计算写出来的那一串形状一致（D98），runner 不必认识两种输入
                due = Due(period_key=period_key(spec.period, as_of), as_of=as_of)
            else:
                try:
                    due = due_for(
                        spec.period,
                        self.calendar,
                        self.settings,
                        market=spec.market,
                        now=moment,
                    )
                except SamsError as exc:
                    # 只说这一个 job 这一轮算不出到期日：其余市场照常跑
                    errors.append(f"{spec.title}: {exc}")
                    continue
                if due is None:
                    # 没到期与算不出是两种结论：前者下一轮可能就到点了，后者是要人去修的，
                    # 混进同一个列表里就分不出该等还是该查
                    waiting.append(spec.title)
                    continue
            results.append(self.runner.execute(spec, due, universe))
        return TickReport(
            now=moment,
            results=tuple(results),
            waiting=tuple(waiting),
            errors=tuple(errors),
        )

    def run_forever(
        self,
        *,
        on_tick: Callable[[TickReport], None] | None = None,
        max_ticks: int | None = None,
    ) -> int:
        """认领心跳，然后一轮一轮跑下去。返回实际跑过的 tick 数。

        这里只接 `SamsError`：那一类是「这一轮做不成」（账本读不到、一个 job 都没选出），
        循环本身没坏，下一轮还得试。别的东西（编程错误）在这里没有可信的下一步，
        炸出去比原地重入更值钱——循环接住它之后，每一拍照打，读的那一侧只会把一个
        坏掉的实现看成「还在跑」。
        """
        self._claim()
        ticks = 0
        try:
            while True:
                try:
                    report = self.tick()
                except SamsError as exc:
                    logger.warning("这一轮 tick 失败：%s", exc)
                    report = TickReport(now=self.clock.now_utc(), errors=(str(exc),))
                if on_tick is not None:
                    on_tick(report)
                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    break
                self.sleep(self.settings.scheduler_tick_s)
        except KeyboardInterrupt:
            logger.warning("收到中断，调度器退出")
        return ticks

    def schedule_data(self) -> ScheduleData:
        return collect(
            runs=self.runs,
            heartbeats=self.heartbeats,
            users=self.users,
            specs=self.jobs,
            clock=self.clock,
        )

    def _claim(self) -> None:
        """认领心跳；已经有别的调度器在跳就拒绝启动（D100 的单实例守卫）。

        只有 `run_forever` 调它：一次手工执行不该留下一个让下一个常驻进程等满阈值
        （`stale_at` 说的那个数）的行。守卫读的是心跳表，不是 pid 文件，所以它挡的是
        「同一个库上的另一个进程」。
        """
        if self._beat_id is not None:
            # 同一个实例再次进循环：上一轮自己认领的那一行不能把自己拒在外面
            return
        latest = self.heartbeats.latest()
        if latest is not None:
            age = (self.clock.now_utc() - latest.beat_at).total_seconds()
            if not stale_at(age):
                raise SchedulerBusy(
                    f"另一个调度器（{latest.id}）{int(age)} 秒前还有心跳："
                    "同一个库上跳两个循环，两个进程都可能在对方写下结论之前"
                    "替同一个周期先下一个结论"
                )
        self._beat_id = self.heartbeats.start(note=f"tick={self.settings.scheduler_tick_s}s")

    def _selected(self, only_job: str | None, only_market: str | None) -> tuple[JobSpec, ...]:
        chosen = tuple(
            spec
            for spec in self.jobs
            if (only_job is None or spec.name == only_job)
            and (only_market is None or spec.market == only_market)
        )
        if chosen:
            return chosen
        wanted = []
        if only_job is not None:
            wanted.append(f"job={only_job}")
        if only_market is not None:
            wanted.append(f"market={only_market}")
        names = sorted({spec.name for spec in self.jobs})
        # 一个都没匹配上是命令敲错了（`wanted`），还是这里本来就没登记 job（「全部」）：
        # 后者不说清就会被读成「这一轮跑完了，只是什么都没做」
        raise ValidationError(
            f"没有匹配的 job（{'、'.join(wanted) or '全部'}）：现有 {names}"
        )
