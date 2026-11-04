"""调度器的健康读数：它还在不在跑、每个 job 上一轮跑成什么。

只读：这一层不写 `task_runs`、不碰心跳，也不决定「该不该放一个常驻循环进来」——
「过期」那条判据归 `heartbeat.py`，这里只是拿它算一个布尔。
「过期」与「停了」不是同一件事：库里还挂着一行还新鲜的 `running` 时，那一拍没翻只说明
某一趟 job 比阈值长（单写者 + 启动时结孤儿使这一句可信，见 `TaskRunRepo.running`），
所以这里把 `stale` 与 `may_be_stopped` 分成两句给读者。
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
from sams.services.scheduler.runner import STALE_RUNNING_H, failure_streak
from sams.services.scheduler.types import JobSpec
from sams.storage.repos.task_runs import Heartbeat, HeartbeatRepo, TaskRun, TaskRunRepo
from sams.storage.repos.users import UserRepo

#: 连续失败最多数出多少个**周期**。它会**饱和**：比这个上限还长的连败显示的是上限本身，
#: 不是真实的周期数——一屏要给的是「停了多久」的量级，而告警在第三个周期就响了（D103），
#: 再往上数不改变任何人该做的事。
STREAK_DISPLAY_PERIODS = 30

#: 一行 `running` 还算「正在做活」的时限，折成秒：超过它，它就不是证据而是崩溃留下的行。
#: 判据与 runner 的「上一轮还在跑」是同一个数（D105），两边一起翻才不会一边说在跑、
#: 另一边说早就死了
STALE_RUNNING_S = STALE_RUNNING_H * 3600


@dataclass(frozen=True)
class JobHealth:
    name: str
    market: str | None
    period: str
    latest: TaskRun | None
    streak: int


@dataclass(frozen=True)
class RunningJob:
    """库里挂着的一趟执行，与它距今多久。

    名字留在读数里而不是只留一个布尔：健康页要说的是「正在跑『财务数据清扫』，已 6 分钟」，
    那句才是「心跳没翻而进程还活着」的凭据。
    """

    name: str
    market: str | None
    period_key: str | None
    age_s: int


@dataclass(frozen=True)
class ScheduleData:
    heartbeat: Heartbeat | None
    beat_age_s: int | None
    admins: int
    jobs: tuple[JobHealth, ...]
    running: tuple[RunningJob, ...] = ()

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

    @property
    def busy(self) -> bool:
        """有一趟 job 正在跑、且还没老到「崩溃留下的行」。

        这是「心跳过期」的反证，但只对**握着库的那一个读者**成立（`/healthz`、健康页）：
        单写者模型下，能读到这行的读者就是写它的那个进程。进程外的那一个（`sams health`）
        一旦能打开库，就说明没人握着锁，那几行只能是上一个进程留下的孤儿（D275）。
        """
        return any(job.age_s < STALE_RUNNING_S for job in self.running)

    @property
    def may_be_stopped(self) -> bool:
        """「它可能停了」只能说在这一句上：过期，且没有还在跑的那一趟做反证。

        只给同进程的读者用（理由见 `busy`）。读数分开、结论也分开是两件事：只看 `stale`
        会把一次长采集报成事故，而报错的事故看多了，人就不再信这一屏。
        """
        return self.stale and not self.busy


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
    running = tuple(
        RunningJob(
            name=row.job_name,
            market=row.market,
            period_key=row.period_key,
            # 下限 0 与那一拍同理：`started_at` 由写的那一侧的时钟落下，未来的时刻
            # 不是「刚起跑」，报负数只是把两台时钟不一致藏进一个看不出原因的数
            age_s=max(0, int((moment - row.started_at).total_seconds())),
        )
        for row in runs.running()
    )
    return ScheduleData(
        heartbeat=beat, beat_age_s=age, admins=admins, jobs=tuple(jobs), running=running
    )
