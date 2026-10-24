"""常驻进程的线程生命周期与 `/healthz` 的读数（spec §2.1/§2.3）。

`Runtime` 不认识组装根：它拿到的是已经装好的调度器、盘中节拍器、清理服务与行情板。
「谁负责接线」因此只有 `bootstrap` 一处，而这一层可以在测试里用四个假对象跑起来。

线程都是守护线程：主线程退出时它们不该把进程钉住。`stop()` 仍然逐个 join——
守护只保证「不阻止退出」，不保证「退出前把这一轮收干净」。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any, Protocol

from sams import __version__
from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.config import Settings
from sams.core.errors import SamsError
from sams.services.intraday import IntradayMonitor
from sams.services.prune import PruneReport, PruneService
from sams.services.quotes import QuoteBoard
from sams.services.scheduler.report import ScheduleData
from sams.services.scheduler.service import Scheduler

logger = logging.getLogger("sams.services.runtime")

#: 每日维护的钟点（UTC）。不跟「最后一个市场收盘后 N 分钟」走：那个判据会随
#: 夏令时在一年里漂两次，而清理不依赖任何一个市场当天有没有开市（D116）
MAINTENANCE_AT = time(3, 30)

#: 盘中与维护线程的排空上限。调度线程另有 `scheduler_tick_s + 30`（§2.1）
WORKER_STOP_TIMEOUT_S = 10.0


class _DbProbe(Protocol):
    def scalar(self, sql: str) -> Any: ...


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


class Runtime:
    def __init__(
        self,
        settings: Settings,
        *,
        scheduler: Scheduler,
        schedule_reading: Callable[[], ScheduleData],
        monitor: IntradayMonitor,
        prune: PruneService,
        board: QuoteBoard | None = None,
        database: _DbProbe | None = None,
        clock: Clock = SYSTEM_CLOCK,
        version: str = __version__,
    ) -> None:
        self.settings = settings
        self.scheduler = scheduler
        #: `/healthz` 跑在 HTTP 线程上，不能直呼 `scheduler.schedule_data()`：
        #: 那会用调度线程那条 DuckDB 连接（D113 的同一道线，换了个入口）。
        #: 所以读法由组装根注入——生产传一条每次新开连接的探针，测试传个 lambda。
        self.schedule_reading = schedule_reading
        self.monitor = monitor
        self.prune = prune
        self.board = board
        #: `/healthz` 用一条只回答「读得到吗」的探针；不传就不报这一项
        self.database = database
        self.clock = clock
        self.version = version
        self.stop_event = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self._started_at: datetime | None = None

    @property
    def threads(self) -> dict[str, threading.Thread]:
        return dict(self._threads)

    def start(self) -> None:
        if self._threads:
            raise RuntimeError("Runtime 只能启动一次：第二次会把同一批线程跑成两份")
        self._started_at = self.clock.now_utc()
        self._threads = {
            "scheduler": threading.Thread(
                target=self.scheduler.run_forever,
                kwargs={"stop": self.stop_event},
                name="sams-scheduler",
                daemon=True,
            ),
            "intraday": threading.Thread(
                target=self.monitor.run_forever,
                kwargs={"stop": self.stop_event},
                name="sams-intraday",
                daemon=True,
            ),
            "maintenance": threading.Thread(
                target=self._maintenance_loop,
                name="sams-maintenance",
                daemon=True,
            ),
        }
        for thread in self._threads.values():
            thread.start()
        logger.info("运行时已启动：%s", "、".join(self._threads))

    def stop(self) -> bool:
        """逐个 join 线程，返回「它们是否都退出了」。

        返回 False 时调用方不得关闭共享资源（库连接等）：那条线程可能正握着它们
        执行，从另一条线程关掉等于在别人手里抽走桌子。
        """
        self.stop_event.set()
        timeouts = {
            # 调度线程可能正卡在一个 job 里：等它收尾，但不等一个坏掉的实现
            "scheduler": float(self.settings.scheduler_tick_s) + 30.0,
            "intraday": WORKER_STOP_TIMEOUT_S,
            "maintenance": WORKER_STOP_TIMEOUT_S,
        }
        clean = True
        for name in ("scheduler", "intraday", "maintenance"):
            thread = self._threads.get(name)
            # `start()` 在第二、三条 `thread.start()` 上失败时，字典里会留下没起过的线程：
            # join 一个没起过的线程会抛 RuntimeError，把真正的启动错误盖掉
            if thread is None or thread.ident is None:
                continue
            thread.join(timeout=timeouts[name])
            if thread.is_alive():
                clean = False
                logger.warning(
                    "%s 线程 %ss 内没有退出，继续停其余部分", name, timeouts[name]
                )
        return clean

    def next_maintenance_at(self, now: datetime) -> datetime:
        today = datetime.combine(now.date(), MAINTENANCE_AT, tzinfo=UTC)
        return today if today > now else today + timedelta(days=1)

    def run_maintenance(self, *, dry_run: bool = False) -> PruneReport:
        report = self.prune.run(dry_run=dry_run)
        logger.info(
            "每日清理%s：%s，合计 %d 行",
            "（预演）" if dry_run else "",
            "、".join(f"{step.name}={step.rows}" for step in report.steps),
            report.total,
        )
        return report

    def healthz(self) -> dict[str, Any]:
        now = self.clock.now_utc()
        db_ok, db_error = self._db_probe()
        scheduler, schedule_stale = self._scheduler_reading()
        intraday = self._intraday_reading(now)
        degraded = (
            not db_ok
            or schedule_stale
            or any(not market["ok"] for market in intraday.values())
        )
        return {
            "status": "degraded" if degraded else "ok",
            "db": {"ok": db_ok, "error": db_error},
            "scheduler": scheduler,
            "intraday": intraday,
            "version": self.version,
            "uptime_s": (
                0
                if self._started_at is None
                else max(0, int((now - self._started_at).total_seconds()))
            ),
        }

    def _db_probe(self) -> tuple[bool, str | None]:
        if self.database is None:
            return True, None
        try:
            self.database.scalar("SELECT 1")
        except Exception as exc:  # 健康检查不能因为一次读失败而自己抛出去
            return False, str(exc)
        return True, None

    def _scheduler_reading(self) -> tuple[dict[str, Any], bool]:
        """调度器的读数；读不出来就是「可能死了」，不是让整份 healthz 变成 500。"""
        try:
            schedule = self.schedule_reading()
        except Exception as exc:
            return {"beat_age_s": None, "stale": True, "error": str(exc)}, True
        return {"beat_age_s": schedule.beat_age_s, "stale": schedule.stale}, schedule.stale

    def _intraday_reading(self, now: datetime) -> dict[str, dict[str, Any]]:
        snapshots = self.board.snapshot() if self.board is not None else {}
        reading: dict[str, dict[str, Any]] = {}
        for market in sorted(self.settings.markets):
            board = snapshots.get(market)
            quotes = [] if board is None else list(board.quotes.values())
            newest = max(quotes, key=lambda quote: quote.ts) if quotes else None
            reading[market] = {
                "last_poll_at": _iso(None if board is None else board.last_poll_at),
                # 未来时刻不报负年龄：那是两边时钟不一致，不是「刚更新过」
                "age_s": (
                    None
                    if newest is None
                    else max(0, int((now - newest.ts).total_seconds()))
                ),
                "source": None if newest is None else newest.source,
                # 从未轮询（休市/刚启动）不是故障；轮过且有错才是。
                # 这一条只看最后一次轮询的结果：线程本身死了它看不见——
                # 那要靠「轮询节拍多久没动」另判（已知边界，计划 2 收口）
                "ok": board is None or board.last_error is None,
            }
        return reading

    def _maintenance_loop(self) -> None:
        self._run_maintenance_guarded()
        while not self.stop_event.is_set():
            now = self.clock.now_utc()
            delay = (self.next_maintenance_at(now) - now).total_seconds()
            if self.stop_event.wait(delay):
                break
            self._run_maintenance_guarded()

    def _run_maintenance_guarded(self) -> None:
        try:
            self.run_maintenance()
        except SamsError:
            # 一次清理失败（盘中被写锁挡住、库暂时只读）不该让明天的清理也没人做
            logger.exception("每日清理失败，下一拍再试")
