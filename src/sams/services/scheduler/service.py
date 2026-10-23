"""`Scheduler`：tick 的顺序、心跳与常驻循环。

一轮 tick 做四件事，顺序就是代码里的顺序：

1. 认领过心跳就打一拍（手工 `tick` 没认领过，什么也不留）；
2. 按 `only_job`/`only_market` 选出这一轮要跑的 job，一个都不匹配就拒绝；
3. 构建这一轮的对象全集——一次 tick 只构建一次，交给每个 job 的是同一份；
4. 按注册顺序逐个 job 判断到期并交给 `JobRunner`，把结果原样交回。

打拍放在做活之前：那一拍证明的是「循环还在转」，不是「这一轮做完了事」。挪到收尾
之后，一轮在打拍之前就失败（账本读不到）会一拍都不留，读的那一侧把「这一轮做不成」
看成「循环停了」——那是两件事：前者是这一轮的结论（`TickReport.errors`），后者才是
那一拍要答的。每个 job 之前再补一拍：整条链可能比阈值长，只在轮首打的那一拍会把
「还在做活」读成「已经停了」。单个 job 自身超过阈值的那一段仍补不了（没有第二个
线程），这条边界写在 `heartbeat.py` 里。

库旁还要写一份同样的拍（`beatfile`，D111）：循环握着库时外部读者打不开库，
`[调度]` 在锁住时唯一能读的就是那个文件。

等待间隔不比心跳阈值短的话，一个空转的循环也会在两次拍之间把自己读成停了，
所以构造时挡住（见 `heartbeat.py`）。

单个 job 的异常在 runner 里已经落账；这里只隔离**到期计算**的异常：
日历源坏了不能让排在后面的市场也不跑。

打印与退出码都不在这里：谁造这个循环、结果怎么给人看，由装配与入口那一侧决定。
"""

from __future__ import annotations

import itertools
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.config import Settings
from sams.core.errors import SamsError, SchedulerBusy, ValidationError
from sams.services.scheduler.beatfile import write_beat
from sams.services.scheduler.due import CalendarView, Due, due_for, local_today, period_key
from sams.services.scheduler.heartbeat import HEARTBEAT_STALE_S, stale_at
from sams.services.scheduler.report import ScheduleData, collect
from sams.services.scheduler.runner import JobRunner, RunResult
from sams.services.scheduler.types import JobSpec
from sams.services.scheduler.universe import Universe, UniverseBuilder
from sams.storage.repos.task_runs import FAILED, HeartbeatRepo, TaskRunRepo
from sams.storage.repos.users import UserRepo

logger = logging.getLogger("sams.services.scheduler.service")


@dataclass(frozen=True)
class RunRequest:
    """一次「现在就跑」的请求：跑哪些 job、锚在哪一天。"""

    id: str
    job: str | None
    market: str | None
    as_of: date | None
    requested_at: datetime


class RunRequestQueue:
    """跨线程的请求队列：Web 只排队，调度线程在下一拍执行。

    `task_runs` 只有一个写入方（D105），所以 Web 的「立即同步」不能另开写线程；
    它把请求放进这里，由同一条调度线程执行。id 用序号而不是时间戳：时钟可能被
    冻住（测试）或两个请求落在同一微秒，而按钮回显要求两个 id 不同。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: deque[RunRequest] = deque()
        self._seq = itertools.count(1)

    def put(
        self,
        *,
        job: str | None,
        market: str | None,
        as_of: date | None,
        requested_at: datetime,
    ) -> RunRequest:
        with self._lock:
            request = RunRequest(
                id=f"req-{next(self._seq)}",
                job=job,
                market=market,
                as_of=as_of,
                requested_at=requested_at,
            )
            self._items.append(request)
            return request

    def drain(self) -> tuple[RunRequest, ...]:
        with self._lock:
            items = tuple(self._items)
            self._items.clear()
            return items

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


@dataclass(frozen=True)
class RequestReport:
    request: RunRequest
    results: tuple[RunResult, ...] = ()


@dataclass(frozen=True)
class TickReport:
    now: datetime
    results: tuple[RunResult, ...] = ()
    waiting: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    #: 手动请求按请求分组；`results` 里也有它们，按执行顺序排在到期 job 之前
    requests: tuple[RequestReport, ...] = ()

    @property
    def failed(self) -> tuple[RunResult, ...]:
        return tuple(result for result in self.results if result.status == FAILED)


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        jobs: Sequence[JobSpec],
        *,
        runner: JobRunner,
        runs: TaskRunRepo,
        heartbeats: HeartbeatRepo,
        universe: UniverseBuilder,
        users: UserRepo,
        calendar: CalendarView,
        clock: Clock = SYSTEM_CLOCK,
        sleep: Callable[[float], None] = time.sleep,
        beat_path: Path | None = None,
        requests: RunRequestQueue | None = None,
    ) -> None:
        if settings.scheduler_tick_s >= HEARTBEAT_STALE_S:
            raise ValidationError(
                f"scheduler_tick_s={settings.scheduler_tick_s} 不小于心跳阈值 "
                f"{HEARTBEAT_STALE_S}：一个空转的循环也会在两次拍之间把自己读成停了，"
                "而读数与守卫用的是同一个比较，两边一起翻错"
            )
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
        #: 库旁那一拍（D111）。库被这个循环握着时，外部读者打不开库，只能读它；
        #: 内存库没有文件，`None`
        self.beat_path = beat_path
        #: 手动请求队列：`serve` 里的 Web 线程与本调度线程共用这一个实例
        self.requests = requests if requests is not None else RunRequestQueue()
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
        self._beat()
        selected = self._selected(only_job, only_market)
        universe = self.universe.build()
        # 手工请求排在到期 job 之前：人点了按钮，这件事先做。清空放在 universe
        # 构建之后——构建失败时请求还留在队列里，下一拍仍能再试
        manual = self._run_requests(self.requests.drain(), universe, moment)
        results: list[RunResult] = [result for report in manual for result in report.results]
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
            # 每个 job 之前补一拍：整条链可能比阈值长，只在轮首打的那一拍会把
            # 「还在做活」读成「已经停了」（D111）。单个 job 自身超过阈值的那一段
            # 仍然补不了——这里没有第二个线程，`heartbeat.py` 把这条边界写明了
            self._beat()
            results.append(self.runner.execute(spec, due, universe))
        return TickReport(
            now=moment,
            results=tuple(results),
            waiting=tuple(waiting),
            errors=tuple(errors),
            requests=manual,
        )

    def request_run(
        self,
        job: str | None = None,
        market: str | None = None,
        *,
        as_of: date | None = None,
    ) -> RunRequest:
        """把手动请求排进下一拍；敲错的名字在这里就拒绝。

        入队前跑一遍与 `tick` 相同的筛选：请求线程还拿得到异常，能当场告诉人哪一段
        敲错了；交给调度线程之后，那条错只剩日志里的一行。
        """
        self._selected(job, market)
        return self.requests.put(
            job=job,
            market=market,
            as_of=as_of,
            requested_at=self.clock.now_utc(),
        )

    @property
    def pending_requests(self) -> int:
        return len(self.requests)

    def _run_requests(
        self,
        requests: Sequence[RunRequest],
        universe: Universe,
        moment: datetime,
    ) -> tuple[RequestReport, ...]:
        """这里绕过的只有到期计算：本周期已成功、上一轮还在跑、失败后十分钟内
        这三条仍由 runner 回答（D105）——它们保护的是源与账本，不是时间表。
        """
        reports: list[RequestReport] = []
        for request in requests:
            results: list[RunResult] = []
            for spec in self._selected(request.job, request.market):
                day = request.as_of or self._anchor_day(spec, moment)
                self._beat()
                results.append(
                    self.runner.execute(
                        spec,
                        Due(period_key=period_key(spec.period, day), as_of=day),
                        universe,
                    )
                )
            reports.append(RequestReport(request=request, results=tuple(results)))
        return tuple(reports)

    def _anchor_day(self, spec: JobSpec, moment: datetime) -> date:
        """手动触发没有「到期日」：锚在这次请求的市场本地今天（D99）。

        全局 job（汇率）没有市场时区，按 `moment` 自己的日期定锚；M4-A 的管理页
        只按市场触发日线，不走这一支。
        """
        if spec.market is None:
            return moment.date()
        return local_today(self.settings, spec.market, moment)

    def run_forever(
        self,
        *,
        on_tick: Callable[[TickReport], None] | None = None,
        max_ticks: int | None = None,
        stop: threading.Event | None = None,
    ) -> int:
        """认领心跳，然后一轮一轮跑下去。返回实际跑过的 tick 数。

        `max_ticks=0`（或负数）一轮都不跑，也不认领心跳：留下一个刚打过的拍会把
        下一个常驻循环挡在阈值外面，而这一次连活都没开始。

        `stop` 是给同进程的 `serve` 用的：它在两拍之间（以及等待中）结束循环，
        不必发信号；有 `stop` 时等待走 `stop.wait`，停止请求不用等满一个间隔。
        没有 `stop` 时仍用注入的 `sleep`，CLI 与既有测试的行为不变。

        这里只接 `SamsError`：那一类是「这一轮做不成」（账本读不到、一个 job 都没选出），
        循环本身没坏，下一轮还得试。别的东西（编程错误）在这里没有可信的下一步，
        炸出去比原地重入更值钱——循环接住它之后，每一拍照打，读的那一侧只会把一个
        坏掉的实现看成「还在跑」。
        """
        if max_ticks is not None and max_ticks <= 0:
            return 0
        if stop is not None and stop.is_set():
            # 与 `max_ticks<=0` 同理：认领一行刚打过的心跳会把下一个循环挡在阈值外
            return 0
        self._claim()
        # 认领之后先打一拍：文件从第一轮开始就该存在，而不是等第一个 job 走到
        self._beat()
        ticks = 0
        try:
            try:
                while max_ticks is None or ticks < max_ticks:
                    if stop is not None and stop.is_set():
                        break
                    try:
                        report = self.tick()
                    except SamsError as exc:
                        # 栈只能留在这里：这一句把异常吞了，`tick` 的那一层不再有人能打印它
                        logger.exception("这一轮 tick 失败：%s", exc)
                        report = TickReport(now=self.clock.now_utc(), errors=(str(exc),))
                    if on_tick is not None:
                        on_tick(report)
                    ticks += 1
                    if max_ticks is not None and ticks >= max_ticks:
                        break
                    if stop is not None:
                        if stop.wait(self.settings.scheduler_tick_s):
                            break
                    else:
                        self.sleep(self.settings.scheduler_tick_s)
            except KeyboardInterrupt:
                logger.warning("收到中断，调度器退出")
            return ticks
        finally:
            # 正常退出要把文件带走：留下一个越来越旧的文件，会把「我停了」说成「我卡住了」。
            # 被 SIGKILL 的那一种留得下，那正是「没人正常收尾」的证据
            self._drop_beat_file()

    def schedule_data(self) -> ScheduleData:
        return collect(
            runs=self.runs,
            heartbeats=self.heartbeats,
            users=self.users,
            specs=self.jobs,
            clock=self.clock,
        )

    def _beat(self) -> None:
        """库里的那一拍与库旁的那一拍一起翻：两处读数不能有一个落后于轮次。

        手工 `tick` 没有认领过心跳（`_beat_id` 为空），什么也不留。
        """
        if self._beat_id is None:
            return
        self.heartbeats.beat(self._beat_id)
        self._write_beat_file()

    def _write_beat_file(self) -> None:
        try:
            write_beat(self.beat_path, pid=os.getpid(), beat_at=self.clock.now_utc())
        except OSError as exc:
            # 库里的那一拍仍然写得进去：文件写不进去只是「外部读者少一条路」，
            # 不是「这一轮不该跑」
            logger.warning("心跳文件写不进去（%s）：库里的那一拍仍然有效", exc)

    def _drop_beat_file(self) -> None:
        if self.beat_path is None:
            return
        try:
            self.beat_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("心跳文件删不掉（%s）：它会在下一次读数里变旧", exc)

    def _claim(self) -> None:
        """认领心跳；已经有一个循环在跳就拒绝启动（D100 的单实例守卫）。

        只有 `run_forever` 调它：一次手工执行不该留下一行，让下一个常驻循环白等
        一个阈值（`HEARTBEAT_STALE_S` 说的那个数）。

        守卫读的是心跳表而不是进程内的一个标记，所以它挡得住「同一个进程里再造一个
        `Scheduler` 去跑常驻循环」。它挡不住另一个操作系统进程：库文件上的锁先就让
        第二个进程连不上（`DatabaseLocked`，D111），那一类重复启动在走到这句守卫之前
        就已经炸在连接上。
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
                    "同一份库上跳两个循环会把同一个周期各跑一遍，而 `task_runs` 里"
                    "那一行是「这个周期跑过没有」的唯一依据（D98）"
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
