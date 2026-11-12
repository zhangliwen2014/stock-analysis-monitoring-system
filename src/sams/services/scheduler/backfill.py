"""补数队列的每拍消费者：至多 limit 条，单票的失败记在 `symbol_backfills` 自己头上。

一行是一种活：`bars` 补日线，`actions` 确认公司行为覆盖（spec 2026-11-03）——后者是
「有人打开了这只票的页面，但它的区间涨跌还没过复权闸」那件事的唯一自动出路：Web 不能替
调度线程去取数，DuckDB 单写者也让用户在 serve 跑着的时候敲不了 `sams sync actions`。

D330 之后还多三种：`financials` / `valuation` / `flow`。它们只由孤源 job 的止损写进来
（`jobs.py::_collect`），所以消费者必须认识它们，否则昨晚顺延的票今天也没有人去补。
这三种的「0 行」都是合法答复（D36）：没有披露、没有数据不代表补数失败。

D339 再多三种档案活：`profile` / `announcements` / `news`。它们由打开个股资料块那一刻
排成 pending（D283），消费者分派到 `F10SyncService` / `NewsService`，窗口与同名 job
同口径；0 行同样是合法答复，吃完即 done。

不进 task_runs 是因为那份历史回答「哪个 job 跑没跑」，这里积压的是单票属性——
混进去之后「bars 跑成功了但三只票还欠着」两种真相分不开。失败三次转 deferred：
今天的重试预算用光，等今晚的盘后日线任务收尾（`SyncService.sync_daily` 的结案钩子）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.config import Settings
from sams.core.errors import DatasourceError, SamsError
from sams.core.timeutil import HISTORY_START
from sams.services.news import NEWS_WINDOW_DAYS
from sams.services.scheduler.due import local_today
from sams.services.scheduler.jobs import (
    ANNOUNCEMENTS_WINDOW_DAYS,
    INITIAL_SYNC_DAYS,
    _window,
)
from sams.storage.repos.symbol_backfills import (
    ACTIONS,
    ANNOUNCEMENTS,
    FINANCIALS,
    FLOW,
    NEWS,
    PROFILE,
    VALUATION,
)

logger = logging.getLogger("sams.services.scheduler.backfill")

#: 泵两次真动手之间的最小间隔（秒，#381 / D313）。
#: 取 20 秒不是为了省 CPU，是为了别让按只循环自己变慢：一次真动手是 3 个请求
#: （每个 1~3 秒），把它塞在每两只票之间会把 505 只的那一趟拖成两倍长——而那条慢下来
#: 的代价是其余那两百多只今天没数。20 秒意味着一条 1~3 秒的循环每六七只票喘一口气。
MIN_GAP_S = 20.0


class BackfillConsumer:
    def __init__(
        self,
        sync,
        corp_actions,
        backfills,
        settings: Settings,
        *,
        limit: int = 3,
        fundamentals=None,
        f10=None,
        news=None,
    ) -> None:
        self.sync = sync
        self.corp_actions = corp_actions
        self.backfills = backfills
        self.settings = settings
        self.limit = limit
        self.fundamentals = fundamentals
        self.f10 = f10
        self.news = news

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
        """跑一行，返回「这次算不算白跑」。

        `bars` 的 0 行是「没补上」，其余几种的 0 行都是合法答复（D36）：公司行为没记录、
        财务没披露、估值/资金流当天没数据，各自都有落库或不落库的证据，不该记成失败。
        """
        if row.kind == ACTIONS:
            # 全历史到今天，不按 730 天：覆盖确认写的是「这只票截至今天的除权记录就这些」，
            # 按窗口拉会留下更早的空白，而闸问的恰恰是那个空白
            self.corp_actions.sync_actions(row.market, row.code, HISTORY_START, end)
            # 0 条公司行为是合法答复：确认照样落 `corp_actions_coverage`，那才是
            # 「这只票没分过红」的证据。日线那边没有这种答复——一根 K 线都没有就是没补上
            return False
        if row.kind == FINANCIALS:
            # 与 job 同一个口径：报告期是重述的，增量窗口会漏掉旧期的修正
            self._fundamentals().sync_financials(
                row.market, row.code, HISTORY_START, end
            )
            return False
        if row.kind in (VALUATION, FLOW):
            return self._catch_up_daily_fundamental(row, end)
        if row.kind == PROFILE:
            # 概况一票一行，窗口对它没有意义（与 run_profile 同口径，D214）
            self._f10().sync_profile(row.market, row.code)
            return False
        if row.kind == ANNOUNCEMENTS:
            self._f10().sync_announcements(
                row.market, row.code, end - timedelta(days=ANNOUNCEMENTS_WINDOW_DAYS), end
            )
            return False
        if row.kind == NEWS:
            self._news().sync(
                row.market, row.code, end - timedelta(days=NEWS_WINDOW_DAYS), end
            )
            return False
        start = end - timedelta(days=INITIAL_SYNC_DAYS)
        return self.sync.sync_daily(row.market, row.code, start, end).empty

    def _fundamentals(self):
        if self.fundamentals is None:
            # 没接服务时不许把它当成 done：那会把一条顺延的账悄悄抹掉
            raise DatasourceError("backfill", "补数消费者没有基本面服务")
        return self.fundamentals

    def _f10(self):
        if self.f10 is None:
            raise DatasourceError("backfill", "补数消费者没有个股资料服务")
        return self.f10

    def _news(self):
        if self.news is None:
            raise DatasourceError("backfill", "补数消费者没有新闻服务")
        return self.news

    def _catch_up_daily_fundamental(self, row, end) -> bool:
        """估值/资金流按库内最新一天续上；已经追平就无事可做（与 job 的 `_window` 同口径）。"""
        service = self._fundamentals()
        when, sync = (
            (service.valuation.latest_date, service.sync_valuation)
            if row.kind == VALUATION
            else (service.flow.latest_date, service.sync_flow)
        )
        start = _window(when(row.market, row.code), end)
        if start is not None:
            sync(row.market, row.code, start, end)
        return False


class BackfillPump:
    """把消费者接到「两只票之间」那个喘息口上的节流阀（#381 / D313）。

    为什么要节流而不是直接叫消费者：`_collect` 会把它叫上五百次（一条 505 只的循环），
    绝大多数应该看一眼就返回。闸落在**上次真的动手**那一刻，不是「上次被调用」——
    一直有队列可拿的那一段若按调用时刻算闸，闸会被自己往后推，反而补得更慢。

    它不是线程也不是第二条 DuckDB 连接：调度线程本来就有 99% 的时间在等网络，
    把其中一小段换成替页面补数，不引入任何并发写（这是这条设计的前提，不是妥协）。
    """

    def __init__(
        self,
        consumer: BackfillConsumer,
        *,
        clock: Clock = SYSTEM_CLOCK,
        monotonic: Callable[[], float] = time.monotonic,
        min_gap_s: float = MIN_GAP_S,
    ) -> None:
        self.consumer = consumer
        self.clock = clock
        self.monotonic = monotonic
        self.min_gap_s = min_gap_s
        #: 下一次允许真动手的单调时刻；None 表示从没动过手
        self._next_at: float | None = None

    def __call__(self) -> int:
        now = self.monotonic()
        if self._next_at is not None and now < self._next_at:
            return 0
        self._next_at = now + self.min_gap_s
        return self.consumer.consume(self.clock.now_utc())
