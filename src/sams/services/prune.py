"""保留策略：把只增不删的三张表按各自的粒度裁掉（D116）。

为什么是一个服务而不是三段 SQL：CLI、常驻进程的每日维护、admin 健康页的按钮
是三个入口，但「保留几天、按什么排序、算多少行」只能有一份答案。dry-run 与实删
必须走同一个计数路径，否则「预演说 10 行、实删 12 行」只是时间差，没人查得出来。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import ValidationError
from sams.storage.db import Database
from sams.storage.repos.quotes import QuoteRepo
from sams.storage.repos.task_runs import OK, HeartbeatRepo, TaskRunRepo

logger = logging.getLogger("sams.services.prune")

#: `sams db prune` 的三个默认值（spec D116）。它们也是服务与 Web 按钮的默认值：
#: 三处各写一个 5/90/1000，改一处就会得到「命令行说 5 天、健康页按钮删 7 天」
KEEP_QUOTE_SESSIONS = 5
KEEP_TASK_RUN_DAYS = 90
KEEP_HEARTBEATS = 1000

#: 每一步在 `task_runs` 里留下的那一行带这个 job 名
JOB_NAME = "prune"


@dataclass(frozen=True)
class PruneStep:
    name: str
    rows: int


@dataclass(frozen=True)
class PruneReport:
    dry_run: bool
    steps: tuple[PruneStep, ...] = ()

    @property
    def total(self) -> int:
        return sum(step.rows for step in self.steps)


class PruneService:
    def __init__(
        self,
        db: Database,
        *,
        quotes: QuoteRepo,
        task_runs: TaskRunRepo,
        heartbeats: HeartbeatRepo,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.db = db
        self.quotes = quotes
        self.task_runs = task_runs
        self.heartbeats = heartbeats
        self.clock = clock

    def run(
        self,
        *,
        quotes_sessions: int = KEEP_QUOTE_SESSIONS,
        task_runs_days: int = KEEP_TASK_RUN_DAYS,
        heartbeats_keep: int = KEEP_HEARTBEATS,
        dry_run: bool = False,
    ) -> PruneReport:
        for name, value in (
            ("quotes_sessions", quotes_sessions),
            ("task_runs_days", task_runs_days),
            ("heartbeats_keep", heartbeats_keep),
        ):
            if value < 1:
                raise ValidationError(f"{name}={value}：至少要 1；0 会把当前这一份全删掉")

        if dry_run:
            steps, _, _ = self._measure(quotes_sessions, task_runs_days, heartbeats_keep)
            return PruneReport(dry_run=True, steps=steps)

        # 计数与删除在同一个事务里：盘中线程随时会写新快照，先数后删的话，
        # 中间落地的一天会把第 N 新的边界推后，实际删掉的行数与报告对不上
        with self.db.transaction():
            steps, day_cutoffs, run_cutoff = self._measure(
                quotes_sessions, task_runs_days, heartbeats_keep
            )
            self.quotes.delete_before(day_cutoffs)
            self.task_runs.delete_before(run_cutoff)
            self.heartbeats.delete_beyond(heartbeats_keep)
            # 台账写在删除之后：清掉九十天前的行，再记今天这三步做了什么
            for step in steps:
                run_id = self.task_runs.start(JOB_NAME)
                self.task_runs.finish(
                    run_id, status=OK, rows_affected=step.rows, note=step.name
                )
                logger.info("prune %s: 删除 %d 行", step.name, step.rows)
        return PruneReport(dry_run=False, steps=steps)

    def _measure(
        self, quotes_sessions: int, task_runs_days: int, heartbeats_keep: int
    ) -> tuple[tuple[PruneStep, ...], dict[str, date], datetime]:
        day_cutoffs = self.quotes.session_cutoffs(quotes_sessions)
        run_cutoff = self.clock.now_utc() - timedelta(days=task_runs_days)
        steps = (
            PruneStep("quotes_snapshot", self.quotes.count_before(day_cutoffs)),
            PruneStep("task_runs", self.task_runs.count_before(run_cutoff)),
            PruneStep(
                "scheduler_heartbeats", self.heartbeats.count_beyond(heartbeats_keep)
            ),
        )
        return steps, day_cutoffs, run_cutoff
