"""常驻进程的线程生命周期与 `/healthz` 的读数（spec §2.1/§2.3）。

`Runtime` 不认识组装根：它拿到的是已经装好的调度器、盘中节拍器、队列的消费者、
清理服务与行情板。
「谁负责接线」因此只有 `bootstrap` 一处，而这一层可以在测试里用几组假对象跑起来。

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


class _PruneRun(Protocol):
    """清理的形状：维护线程那台 `PruneService` 与手动那条自带连接的 runner 都长这样。"""

    def run(self, *, dry_run: bool = False) -> PruneReport: ...


class _Worker(Protocol):
    """通知线程的形状（结构化）：`NotifyWorker` 与测试替身各长各的。"""

    def run(self, *, stop: threading.Event) -> None: ...


#: 没接手动清理时念的那一句。这里不退回 `self.prune`：那一条连的是维护线程自己的
#: 连接，借它写库是 D113 要挡的事故，而那句话屏幕上读得出「这个装配还差一根线」
NO_MANUAL_PRUNE = "这个运行时没有接手动清理的连接"


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
        manual_prune: _PruneRun | None = None,
        worker: _Worker | None = None,
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
        # 维护那一拍用的清理服务：它的连接属于 `sams-maintenance` 那条线程
        self.prune = prune
        #: 手动那一下用的清理，得自带一条连接：借 `self.prune` 那条就是两条线程共用
        #: 一个 `Database`，而它的 `_tx_depth` 没有计数锁——03:30 那一拍与手动这一下
        #: 撞上时，先结束的那边会把另一边正在写的事务一起 COMMIT，半套删除被报成完整的
        self.manual_prune = manual_prune
        #: 通知线程的消费者：不传就没有第四条线程（CLI 与只跑三条的测试照旧）
        self.worker = worker
        self.board = board
        #: `/healthz` 用一条只回答「读得到吗」的探针；不传就不报这一项
        self.database = database
        self.clock = clock
        self.version = version
        self.stop_event = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self._started_at: datetime | None = None
        self._prune_lock = threading.Lock()
        self._prune_running = False
        #: 手动清理那条线程：不进 `_threads`（那里装的是 `start()` 起一次的服务线程，
        #: 别处按四条断言），但 `stop()` 照样要 join 它——理由写在 `stop()` 的文档里
        self._prune_thread: threading.Thread | None = None
        #: 手动清理的结果三件套：只给健康页看的尽力而为状态——写的是那条清理线程、
        #: 读的是 HTTP 线程，两边都不等对方，所以屏幕上随时可能是上一轮的旧值
        self.last_prune: str | None = None
        self.last_prune_at: datetime | None = None
        self.last_prune_error: str | None = None

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
        if self.worker is not None:
            self._threads["notify"] = threading.Thread(
                target=self.worker.run,
                kwargs={"stop": self.stop_event},
                name="sams-notify",
                daemon=True,
            )
        for thread in self._threads.values():
            thread.start()
        logger.info("运行时已启动：%s", "、".join(self._threads))

    def stop(self) -> bool:
        """逐个 join 线程，返回「它们是否都退出了」。

        返回 False 时调用方不得关闭共享资源（库连接等）：那条线程可能正握着它们
        执行，从另一条线程关掉等于在别人手里抽走桌子。

        手动清理那条线程不在 `_threads` 里（那里装的是 `start()` 起一次的服务线程），
        但它照样握着库连接，所以照样 join：`sams serve` 停机时一次写了一半的清理
        不能被关在脚下的连接继续跑。
        """
        self.stop_event.set()
        timeouts = {
            # 调度线程可能正卡在一个 job 里：等它收尾，但不等一个坏掉的实现
            "scheduler": float(self.settings.scheduler_tick_s) + 30.0,
            "intraday": WORKER_STOP_TIMEOUT_S,
            "notify": WORKER_STOP_TIMEOUT_S,
            "maintenance": WORKER_STOP_TIMEOUT_S,
        }
        clean = True
        # notify 排在 intraday 之后、maintenance 之前：先停产出告警的线程，
        # 再排空队列，最后才轮到维护
        for name in ("scheduler", "intraday", "notify", "maintenance"):
            thread = self._threads.get(name)
            # `start()` 在后面某条 `thread.start()` 上失败时，字典里会留下没起过的线程：
            # join 一个没起过的线程会抛 RuntimeError，把真正的启动错误盖掉
            if thread is None or thread.ident is None:
                continue
            thread.join(timeout=timeouts[name])
            if thread.is_alive():
                clean = False
                logger.warning(
                    "%s 线程 %ss 内没有退出，继续停其余部分", name, timeouts[name]
                )
        # 手动清理（`request_prune` 起的）不在服务线程那一批里，但它同样握着库连接：
        # 少了这一 join，停机时那次写了一半的清理就跑在已经被关掉的连接上
        prune_thread = self._prune_thread
        if prune_thread is not None and prune_thread.ident is not None:
            prune_thread.join(timeout=WORKER_STOP_TIMEOUT_S)
            if prune_thread.is_alive():
                clean = False
                logger.warning(
                    "手动清理线程 %ss 内没有退出：这次停机不算干净", WORKER_STOP_TIMEOUT_S
                )
        return clean

    def next_maintenance_at(self, now: datetime) -> datetime:
        today = datetime.combine(now.date(), MAINTENANCE_AT, tzinfo=UTC)
        return today if today > now else today + timedelta(days=1)

    def run_maintenance(self, *, dry_run: bool = False) -> PruneReport:
        return self._run_prune(self.prune, dry_run=dry_run)

    def _run_prune(self, service, *, dry_run: bool) -> PruneReport:
        """两条线程跑的是同一套保留策略，日志也就得是同一句。"""
        report = service.run(dry_run=dry_run)
        logger.info(
            "每日清理%s：%s，合计 %d 行",
            "（预演）" if dry_run else "",
            "、".join(f"{step.name}={step.rows}" for step in report.steps),
            report.total,
        )
        return report

    def request_prune(self) -> bool:
        """管理员手动触发的每日清理；已经在跑时返回 False：不排队、不开第二把枪。

        DuckDB 单写：清理线程与调度线程抢库锁会互相等，把「正在跑」挡在入口
        比让两条线程在锁上排队好。
        """
        if self.stop_event.is_set():
            # 收尾已经走到 join 那一段之后：这时起的线程没人等，`stop()` 返回后
            # 调用方会去关库，而它可能正握着那条连接写。宁可这一按不跑
            return False
        with self._prune_lock:
            if self._prune_running:
                return False
            self._prune_running = True
            thread = threading.Thread(
                target=self._prune_once, name="sams-prune-manual", daemon=True
            )
            # 句柄在起线程之前就存下：`stop()` 可能在这条线程刚起、还没写属性时来 join
            self._prune_thread = thread
        try:
            thread.start()
        except BaseException:
            # 起不动时（系统线程见底）这条线程永远不会跑，也就没人归还忙标志：此后
            # 每次手动清理都答「正在跑」，而其实一条线程都不存在。异常照原样冒出去——
            # 那是这台机器的资源问题，不该被页面上那句「正在跑」抹平
            with self._prune_lock:
                self._prune_running = False
                if self._prune_thread is thread:
                    self._prune_thread = None
            raise
        return True

    def _prune_once(self) -> None:
        try:
            if self.manual_prune is None:
                raise SamsError(NO_MANUAL_PRUNE)
            report = self._run_prune(self.manual_prune, dry_run=False)
            self.last_prune = f"{len(report.steps)} 步、合计 {report.total} 行"
            self.last_prune_error = None
        except Exception as exc:  # 失败要留在页面上，不是炸掉后台线程
            # 失败这一次不许留着上一轮的摘要：健康页把那句与这一轮的时间戳并排印，
            # 读出来就成了「上一次真删了 3 行」，而这一次的删除可能只做了一半
            self.last_prune = None
            self.last_prune_error = str(exc)
        finally:
            # 先还忙标志再写时间戳：等 `last_prune_at` 的读者（测试与页面）看到值时，
            # 入口必须已经能收下一按；反过来时那一按会被「正在跑」挡在门外
            with self._prune_lock:
                self._prune_running = False
            self.last_prune_at = self.clock.now_utc()

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
