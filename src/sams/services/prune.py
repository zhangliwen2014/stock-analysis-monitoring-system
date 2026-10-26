"""保留策略：把只增不删的四张表按各自的粒度裁掉（D116，SP1 终审把 done 补数行也接了进来）。

为什么是一个服务而不是四段 SQL：CLI 现在就有；常驻进程的每日维护与 admin 健康页的
按钮在 Web 页面（计划 3）里接进来。三个入口，但「保留几天、按什么排序、算多少行」
只能有一份答案。dry-run 与实删
必须走同一个计数路径，否则「预演说 10 行、实删 12 行」只是时间差，没人查得出来。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import ValidationError
from sams.services.annotation_prune import (
    ORPHAN_DAYS,
    PURGED_DAYS,
    AnnotationPruner,
    AnnotationPruneReport,
)
from sams.storage.db import Database
from sams.storage.repos.quotes import QuoteRepo
from sams.storage.repos.symbol_backfills import SymbolBackfillRepo
from sams.storage.repos.task_runs import OK, HeartbeatRepo, TaskRunRepo

logger = logging.getLogger("sams.services.prune")

#: `sams db prune` 的四个默认值（spec D116）。它们也是 Web 页面（计划 3）那个按钮的默认值：
#: 三处各写一个 5/90/1000，改一处就会得到「命令行说 5 天、健康页按钮删 7 天」
KEEP_QUOTE_SESSIONS = 5
KEEP_TASK_RUN_DAYS = 90
KEEP_HEARTBEATS = 1000
#: done 补数行的保留天数与 task_runs 同刻度：补数台账跟着执行台账一起翻旧账才对着
KEEP_BACKFILL_DONE_DAYS = 90

#: 每一步在 `task_runs` 里留下的那一行带这个 job 名
JOB_NAME = "prune"


@dataclass(frozen=True)
class PruneStep:
    name: str
    rows: int
    #: 计量单位：删文件的步数不是「行」，混进合计会让「预计 N 行」看起来像在数别的东西
    unit: str = "行"


@dataclass(frozen=True)
class PruneReport:
    dry_run: bool
    steps: tuple[PruneStep, ...] = ()
    #: 附件文件数/字节数：与 `rows` 分开报——「删了 3 行」与「删了 3 个文件名下的 5 个文件」
    #: 是两件事，混成一个数会让 dry-run 的预估看起来像在数别的东西
    files: int = 0
    file_bytes: int = 0

    @property
    def total(self) -> int:
        return sum(step.rows for step in self.steps if step.unit == "行")


class PruneService:
    def __init__(
        self,
        db: Database,
        *,
        quotes: QuoteRepo,
        task_runs: TaskRunRepo,
        heartbeats: HeartbeatRepo,
        backfills: SymbolBackfillRepo,
        annotations: AnnotationPruner | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.db = db
        self.quotes = quotes
        self.task_runs = task_runs
        self.heartbeats = heartbeats
        self.backfills = backfills
        self.annotations = annotations
        self.clock = clock

    def run(
        self,
        *,
        quotes_sessions: int = KEEP_QUOTE_SESSIONS,
        task_runs_days: int = KEEP_TASK_RUN_DAYS,
        heartbeats_keep: int = KEEP_HEARTBEATS,
        backfill_done_days: int = KEEP_BACKFILL_DONE_DAYS,
        annotations_purged_days: int = PURGED_DAYS,
        attachments_orphan_days: int = ORPHAN_DAYS,
        dry_run: bool = False,
    ) -> PruneReport:
        for name, value in (
            ("quotes_sessions", quotes_sessions),
            ("task_runs_days", task_runs_days),
            ("heartbeats_keep", heartbeats_keep),
            ("backfill_done_days", backfill_done_days),
            ("annotations_purged_days", annotations_purged_days),
            ("attachments_orphan_days", attachments_orphan_days),
        ):
            if value < 1:
                raise ValidationError(f"{name}={value}：至少要 1；0 会把当前这一份全删掉")

        annotation_trim = (
            self.annotations.run(
                purged_days=annotations_purged_days,
                orphan_days=attachments_orphan_days,
                dry_run=dry_run,
            )
            if self.annotations is not None
            else AnnotationPruneReport()
        )

        if dry_run:
            steps, _, _, _ = self._measure(
                quotes_sessions, task_runs_days, heartbeats_keep, backfill_done_days
            )
            return PruneReport(
                dry_run=True,
                steps=steps + _annotation_steps(annotation_trim),
                files=annotation_trim.attachments + annotation_trim.orphan_files,
                file_bytes=annotation_trim.bytes,
            )

        # 计数与删除走同一个事务：中间失败时已删的行随事务一起回来（回滚用例钉着）。
        # 盘中线程随时会写新快照，但快照隔离让本事务看不到它们——新行永远比截止日新，
        # 既不进计数也不进删除，报告与实际删掉的行数因此一致
        with self.db.transaction():
            steps, day_cutoffs, run_cutoff, backfill_cutoff = self._measure(
                quotes_sessions, task_runs_days, heartbeats_keep, backfill_done_days
            )
            self.quotes.delete_before(day_cutoffs)
            self.task_runs.delete_before(run_cutoff)
            self.heartbeats.delete_beyond(heartbeats_keep)
            self.backfills.delete_done_before(backfill_cutoff)
            # 台账写在删除之后：清掉九十天前的行，再记今天这四步做了什么
            for step in steps:
                run_id = self.task_runs.start(JOB_NAME)
                self.task_runs.finish(
                    run_id, status=OK, rows_affected=step.rows, note=step.name
                )
                logger.info("prune %s: 删除 %d 行", step.name, step.rows)
        if self.annotations is not None:
            for step in _annotation_steps(annotation_trim):
                run_id = self.task_runs.start(JOB_NAME)
                self.task_runs.finish(
                    run_id, status=OK, rows_affected=step.rows, note=step.name
                )
        return PruneReport(
            dry_run=False,
            steps=steps + _annotation_steps(annotation_trim),
            files=annotation_trim.attachments + annotation_trim.orphan_files,
            file_bytes=annotation_trim.bytes,
        )

    def _measure(
        self,
        quotes_sessions: int,
        task_runs_days: int,
        heartbeats_keep: int,
        backfill_done_days: int,
    ) -> tuple[tuple[PruneStep, ...], dict[str, date], datetime, datetime]:
        day_cutoffs = self.quotes.session_cutoffs(quotes_sessions)
        run_cutoff = self.clock.now_utc() - timedelta(days=task_runs_days)
        backfill_cutoff = self.clock.now_utc() - timedelta(days=backfill_done_days)
        steps = (
            PruneStep("quotes_snapshot", self.quotes.count_before(day_cutoffs)),
            PruneStep("task_runs", self.task_runs.count_before(run_cutoff)),
            PruneStep(
                "scheduler_heartbeats", self.heartbeats.count_beyond(heartbeats_keep)
            ),
            PruneStep("symbol_backfills", self.backfills.count_done_before(backfill_cutoff)),
        )
        return steps, day_cutoffs, run_cutoff, backfill_cutoff


def _annotation_steps(report: AnnotationPruneReport) -> tuple[PruneStep, ...]:
    """零行不占一步：旧库的输出与今天一模一样，新增的三步只在这台机器真的用过批注时出现。"""
    steps = (
        PruneStep("annotations_purged", report.annotations),
        PruneStep("attachments_purged", report.attachments),
        PruneStep("attachments_orphan", report.orphan_files, unit="个文件"),
    )
    return tuple(step for step in steps if step.rows)
