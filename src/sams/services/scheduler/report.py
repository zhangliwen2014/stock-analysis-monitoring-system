"""调度器的健康读数：它还在不在跑、每个 job 上一轮跑成什么。

只读：这一层不写 `task_runs`、不碰心跳，也不决定「该不该放一个常驻循环进来」——
「过期」那条判据归 `heartbeat.py`，这里只是拿它算一个布尔。
`streak` 与 runner 用的是同一个函数、同一个「每周期一行」的形状：读数里数出来的
连续失败必须与发不发 `SystemAlarm` 的口径是同一条（D103），否则「显示连败而管理员
一条都没收到」会查不出原因。
谁把这些数打印出来、退出码怎么定，由读它的那一侧决定。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.services.actors import admin_users
from sams.services.scheduler.heartbeat import stale_at
from sams.services.scheduler.runner import failure_streak
from sams.services.scheduler.types import JobSpec
from sams.storage.repos.task_runs import Heartbeat, HeartbeatRepo, TaskRun, TaskRunRepo
from sams.storage.repos.users import UserRepo

#: 连续失败最多数出多少个**周期**。它会**饱和**：比这个上限还长的连败显示的是上限本身，
#: 不是真实的周期数——一屏要给的是「停了多久」的量级，而告警在第三个周期就响了（D103），
#: 再往上数不改变任何人该做的事。
STREAK_DISPLAY_PERIODS = 30


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
        # 下限 0：那一拍可能由一个跑在前面的时钟写下（两个各自的 `Clock`、或系统时钟被
        # 校正过），负年龄是一个看不出原因的数
        age = max(0, int((moment - beat.beat_at).total_seconds()))
    admins = len(admin_users(users))
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
