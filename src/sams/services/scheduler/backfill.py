"""补数队列的每拍消费者：至多 limit 条，单票的失败记在 `symbol_backfills` 自己头上。

一行是一种活：`bars` 补日线，`actions` 确认公司行为覆盖（spec 2026-11-03）——后者是
「有人打开了这只票的页面，但它的区间涨跌还没过复权闸」那件事的唯一自动出路：Web 不能替
调度线程去取数，DuckDB 单写者也让用户在 serve 跑着的时候敲不了 `sams sync actions`。

不进 task_runs 是因为那份历史回答「哪个 job 跑没跑」，这里积压的是单票属性——
混进去之后「bars 跑成功了但三只票还欠着」两种真相分不开。失败三次转 deferred：
今天的重试预算用光，等今晚的盘后日线任务收尾（`SyncService.sync_daily` 的结案钩子）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sams.core.config import Settings
from sams.core.errors import SamsError
from sams.core.timeutil import HISTORY_START
from sams.services.scheduler.due import local_today
from sams.services.scheduler.jobs import INITIAL_SYNC_DAYS
from sams.storage.repos.symbol_backfills import ACTIONS

logger = logging.getLogger("sams.services.scheduler.backfill")


class BackfillConsumer:
    def __init__(
        self, sync, corp_actions, backfills, settings: Settings, *, limit: int = 3
    ) -> None:
        self.sync = sync
        self.corp_actions = corp_actions
        self.backfills = backfills
        self.settings = settings
        self.limit = limit

    def consume(self, moment: datetime) -> int:
        processed = 0
        for row in self.backfills.take_pending(self.limit):
            end = local_today(self.settings, row.market, moment)
            try:
                empty = self._fetch(row, end)
            except SamsError as exc:
                self.backfills.note_failure(row.id, str(exc))
                processed += 1
                continue
            if empty:
                self.backfills.note_failure(row.id, "区间内所有源均无数据")
            else:
                self.backfills.mark_done(row.id)
            processed += 1
        return processed

    def _fetch(self, row, end) -> bool:
        """跑一行，返回「这次算不算白跑」。"""
        if row.kind == ACTIONS:
            # 全历史到今天，不按 730 天：覆盖确认写的是「这只票截至今天的除权记录就这些」，
            # 按窗口拉会留下更早的空白，而闸问的恰恰是那个空白
            self.corp_actions.sync_actions(row.market, row.code, HISTORY_START, end)
            # 0 条公司行为是合法答复：确认照样落 `corp_actions_coverage`，那才是
            # 「这只票没分过红」的证据。日线那边没有这种答复——一根 K 线都没有就是没补上
            return False
        start = end - timedelta(days=INITIAL_SYNC_DAYS)
        return self.sync.sync_daily(row.market, row.code, start, end).empty
