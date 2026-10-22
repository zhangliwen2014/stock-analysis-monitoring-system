"""`sams health` 的 `[调度]` 一节要读的数：它还在不在跑、每个 job 上一轮跑成什么。

只读：这一层不写 `task_runs`、不碰心跳，也不替调度器判断「该不该跑」。
`streak` 与 runner 用的是同一个函数、同一个「每周期一行」的形状——页面上数出来的
连续失败必须与发不发 `SystemAlarm` 的口径是同一条（D103），否则「页面显示连败而
管理员一条都没收到」会查不出原因。
这里只把读数取齐：谁打印它、退出码怎么定，由读它的那一侧决定。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.services.actors import active_users
from sams.services.scheduler.runner import failure_streak
from sams.services.scheduler.types import JobSpec
from sams.storage.repos.task_runs import Heartbeat, HeartbeatRepo, TaskRun, TaskRunRepo
from sams.storage.repos.users import UserRepo

#: 心跳的年龄到这一步就算「可能已经停了」（`stale_at`）；D100 的单实例守卫放第二个
#: 常驻进程进来之前等的就是同一个数。它必须长于一轮 tick（`scheduler_tick_s`），
#: 否则还在做活的进程会被读成停了；又要短到一次崩溃后的重启不必先干等。
HEARTBEAT_STALE_S = 300
#: 连续失败最多数出多少个**周期**。它会**饱和**：比这个上限还长的连败显示的是上限本身，
#: 不是真实的周期数——一屏要给的是「停了多久」的量级，而告警在第三个周期就响了（D103），
#: 再往上数不改变任何人该做的事。
STREAK_DISPLAY_PERIODS = 30


def stale_at(age_seconds: float) -> bool:
    """那一拍是不是已经过期。判据只写在这一处：
    `ScheduleData.stale`（读数里的那一个布尔）与 `Scheduler._claim`（放不放第二个
    常驻进程进来）走的是同一个比较，否则恰好踩在阈值上时读数说「没停」，
    而另一个进程已经进来把同一个周期再告一遍。
    """
    return age_seconds >= HEARTBEAT_STALE_S


@dataclass(frozen=True)
class JobHealth:
    name: str
    market: str | None
    period: str
    latest: TaskRun | None
    streak: int


@dataclass(frozen=True)
class ScheduleData:
    heartbeat: Heartbeat | None
    beat_age_s: int | None
    admins: int
    jobs: tuple[JobHealth, ...]

    @property
    def stale(self) -> bool:
        """有过心跳、且已经过期：「它可能死了」只有这一条判据。
        发现它停了归外部（D101），所以这里只给读数，不发任何东西。
        """
        return self.beat_age_s is not None and stale_at(self.beat_age_s)

    @property
    def never_started(self) -> bool:
        """没有那一行：与「有过但停了」是两件事，前者不该被读成一次事故。"""
        return self.heartbeat is None


def collect(
    *,
    runs: TaskRunRepo,
    heartbeats: HeartbeatRepo,
    users: UserRepo,
    specs: Sequence[JobSpec],
    clock: Clock = SYSTEM_CLOCK,
) -> ScheduleData:
    moment = clock.now_utc()
    beat = heartbeats.latest()
    age = None
    if beat is not None:
        # 下限 0：那一拍可能由另一个进程写下，而它的时钟跑在读取侧前面
        age = max(0, int((moment - beat.beat_at).total_seconds()))
    admins = sum(1 for user in active_users(users) if user.is_admin)
    jobs = []
    for spec in specs:
        # 按周期分组：一个一直失败的周期每轮重试都多写一行，按行数窗口会被它自己占满
        periods = runs.latest_per_period(spec.name, spec.market, limit=STREAK_DISPLAY_PERIODS)
        jobs.append(
            JobHealth(
                name=spec.name,
                market=spec.market,
                period=spec.period,
                latest=periods[0] if periods else None,
                streak=failure_streak(periods),
            )
        )
    return ScheduleData(heartbeat=beat, beat_age_s=age, admins=admins, jobs=tuple(jobs))
