"""补数队列的每拍消费者：至多 limit 条，单票的失败记在 `symbol_backfills` 自己头上。

不进 task_runs 是因为那份历史回答「哪个 job 跑没跑」，这里积压的是单票属性——
混进去之后「bars 跑成功了但三只票还欠着」两种真相分不开。失败三次转 deferred：
今天的重试预算用光，等今晚的盘后日线任务收尾（`SyncService.sync_daily` 的结案钩子）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sams.core.config import Settings
from sams.core.errors import SamsError
from sams.services.scheduler.due import local_today
from sams.services.scheduler.jobs import INITIAL_SYNC_DAYS

logger = logging.getLogger("sams.services.scheduler.backfill")


class BackfillConsumer:
    def __init__(self, sync, backfills, settings: Settings, *, limit: int = 3) -> None:
        self.sync = sync
        self.backfills = backfills
        self.settings = settings
        self.limit = limit

    def consume(self, moment: datetime) -> int:
        processed = 0
        for row in self.backfills.take_pending(self.limit):
            end = local_today(self.settings, row.market, moment)
            start = end - timedelta(days=INITIAL_SYNC_DAYS)
            try:
                summary = self.sync.sync_daily(row.market, row.code, start, end)
            except SamsError as exc:
                self.backfills.note_failure(row.id, str(exc))
                processed += 1
                continue
            if summary.empty:
                self.backfills.note_failure(row.id, "区间内所有源均无数据")
            else:
                self.backfills.mark_done(row.id)
            processed += 1
        return processed
