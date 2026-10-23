"""取价、写快照、更新行情板、记报价源健康（spec §4.2/§4.3）。

这一层不认识「现在是不是交易时段」：时段判断在 `IntradayMonitor`，它只在该问的时候
调用 `poll`。也不认识页面：页面读 `QuoteBoard`，不读本类。

顺序是：取价 → 落库（失败重试后仍失败就记日志）→ 更新板 → 发事件。
落库失败不回滚板：这一轮的价格仍然是事实，页面先服务人，下一轮会重写同一只票。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import SamsError, StorageError
from sams.core.events import EventBus, QuoteUpdated, SystemAlarm
from sams.core.quotes import Quote, QuoteSourceRecord
from sams.core.retry import Sleeper, call_with_backoff
from sams.datasource.quote import QUOTE_BATCH, QuoteAdapter
from sams.datasource.ratelimit import TokenBucket
from sams.datasource.registry import AdapterRegistry
from sams.storage.repos.quotes import QuoteHealthRepo, QuoteRepo

logger = logging.getLogger("sams.services.quotes")

#: 整市场连续这么久没有任何报价才告警（spec §4.2）：一次超时不是事故
QUOTE_OUTAGE_ALARM_S = 180


@dataclass(frozen=True)
class MarketBoard:
    """一个市场此刻的读视图。`quotes` 按代码索引，是副本。"""

    market: str
    quotes: dict[str, Quote] = field(default_factory=dict)
    last_poll_at: datetime | None = None
    last_error: str | None = None
    #: 非交易时段/午休被跳过时的那一句；它解释「为什么没有新报价」，不是故障
    skip_reason: str | None = None


@dataclass
class _Mark:
    last_poll_at: datetime | None = None
    last_error: str | None = None
    skip_reason: str | None = None


class QuoteBoard:
    """进程内的「最后已知价」。

    读写都加锁：写方是盘中轮询线程，读方是每个页面的请求线程。`snapshot()` 返回
    深拷贝的字典（`Quote` 本身不可变），调用方改它不会碰到板子。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._quotes: dict[tuple[str, str], Quote] = {}
        self._marks: dict[str, _Mark] = {}

    def seed(self, quotes: Iterable[Quote]) -> None:
        with self._lock:
            for quote in quotes:
                self._quotes[(quote.market, quote.code)] = quote

    def update(
        self,
        market: str,
        quotes: Iterable[Quote],
        *,
        now: datetime,
        error: str | None = None,
    ) -> None:
        with self._lock:
            for quote in quotes:
                self._quotes[(quote.market, quote.code)] = quote
            mark = self._marks.setdefault(market, _Mark())
            mark.last_poll_at = now
            mark.last_error = error
            mark.skip_reason = None

    def fail(self, market: str, error: str, *, now: datetime) -> None:
        """整市场取价失败：保留上一笔并显式标 stale。

        `quote_state` 也会按年龄判 stale，但「这一轮全都失败」比「价格十分钟没动」
        更强：源挂了时页面必须马上变色，而不是等 `quote_stale_s` 过去。
        """
        with self._lock:
            for key, quote in list(self._quotes.items()):
                if key[0] == market:
                    self._quotes[key] = quote.marked_stale()
            mark = self._marks.setdefault(market, _Mark())
            mark.last_poll_at = now
            mark.last_error = error
            mark.skip_reason = None

    def skip(self, market: str, reason: str, *, now: datetime) -> None:
        with self._lock:
            mark = self._marks.setdefault(market, _Mark())
            mark.last_poll_at = now
            mark.last_error = None
            mark.skip_reason = reason

    def get(self, market: str, code: str) -> Quote | None:
        with self._lock:
            return self._quotes.get((market, code))

    def snapshot(self) -> dict[str, MarketBoard]:
        with self._lock:
            markets = sorted(set(self._marks) | {market for market, _ in self._quotes})
            result: dict[str, MarketBoard] = {}
            for market in markets:
                mark = self._marks.get(market) or _Mark()
                result[market] = MarketBoard(
                    market=market,
                    quotes={
                        code: quote
                        for (quoted_market, code), quote in self._quotes.items()
                        if quoted_market == market
                    },
                    last_poll_at=mark.last_poll_at,
                    last_error=mark.last_error,
                    skip_reason=mark.skip_reason,
                )
            return result


@dataclass(frozen=True)
class QuotePollResult:
    """一整条报价链跑完后的结论。

    与 `QuoteBatch` 差在「跨源」这一维：`QuoteBatch` 是一个源的答复，
    这里汇总了降级过程中所有尝试。
    """

    quotes: tuple[Quote, ...] = ()
    skipped: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    #: 这一轮真正给出报价的源（按尝试顺序）；全失败时为空
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Attempt:
    quotes: tuple[Quote, ...] = ()
    skipped: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    failure: str | None = None


class QuoteService:
    def __init__(
        self,
        registry: AdapterRegistry,
        board: QuoteBoard,
        quotes: QuoteRepo,
        health: QuoteHealthRepo,
        *,
        clock: Clock = SYSTEM_CLOCK,
        bucket: TokenBucket | None = None,
        bus: EventBus | None = None,
        batch: int = QUOTE_BATCH,
        alarm_after_s: int = QUOTE_OUTAGE_ALARM_S,
        sleep: Sleeper = time.sleep,
    ) -> None:
        self.registry = registry
        self.board = board
        self.quotes = quotes
        self.health = health
        self.clock = clock
        #: 与盘后同步共用同一个桶：两条线程各自限速等于把 `rate_limit_per_s` 放大一倍
        self.bucket = bucket
        self.bus = bus
        self.batch = batch
        self.alarm_after_s = alarm_after_s
        self.sleep = sleep
        previous = health.load()
        self._failures = {name: record.consecutive_failures for name, record in previous.items()}
        self._last_ok = {name: record.last_ok_at for name, record in previous.items()}
        self._outage_since: dict[str, datetime] = {}
        self._alarmed: set[str] = set()

    def load_board(self) -> int:
        """启动时用库里每只票最近一笔填板：重启后页面不该从空白开始。"""
        quotes = self.quotes.latest()
        self.board.seed(quotes)
        return len(quotes)

    def poll(
        self, market: str, codes: Sequence[str], *, now: datetime | None = None
    ) -> QuotePollResult:
        moment = now or self.clock.now_utc()
        wanted = list(dict.fromkeys(codes))
        if not wanted:
            return QuotePollResult()
        chain = self.registry.chain_for(market, "quote")
        if not chain:
            message = f"{market} 没有任何支持 quote 能力的报价源"
            self.board.fail(market, message, now=moment)
            self._note_outage(market, moment, got=False)
            return QuotePollResult(errors=(message,))

        collected: list[Quote] = []
        skipped: list[str] = []
        errors: list[str] = []
        degraded: list[str] = []
        answered: list[str] = []
        remaining = list(wanted)
        for name in chain:
            if not remaining:
                break
            adapter = self.registry.adapter(name)
            attempt = self._attempt(name, adapter, market, remaining)
            if attempt.failure is None:
                self._record_source(name, adapter, ok=True, error=None, moment=moment)
                collected.extend(attempt.quotes)
                skipped.extend(attempt.skipped)
                errors.extend(attempt.errors)
                answered.append(name)
                # 这个源回答了（哪怕只回了一部分）：跳过的代码下一轮再试，
                # 换成下一个源等于替用户猜「另一家更可能有这只票」
                break
            collected.extend(attempt.quotes)
            degraded.append(f"{name}: {attempt.failure}")
            errors.append(f"{name}: {attempt.failure}")
            self._record_source(name, adapter, ok=False, error=attempt.failure, moment=moment)
            got = {quote.code for quote in attempt.quotes} | set(attempt.skipped)
            remaining = [code for code in remaining if code not in got]

        summary = "；".join(degraded) or None
        if collected:
            self._persist(market, collected, moment)
            self.board.update(market, collected, now=moment, error=summary)
            self._note_outage(market, moment, got=True)
        else:
            self.board.fail(market, summary or f"{market} 本轮没有取得任何报价", now=moment)
            self._note_outage(market, moment, got=False)
            self._persist(market, [], moment)

        if self.bus is not None:
            for quote in collected:
                self.bus.publish(
                    QuoteUpdated(
                        ts=quote.ts,
                        market=quote.market,
                        code=quote.code,
                        price=quote.price,
                        pct_chg=quote.pct_chg,
                        volume=quote.volume,
                        is_stale=quote.is_stale,
                    )
                )
        return QuotePollResult(
            quotes=tuple(collected),
            skipped=tuple(skipped),
            errors=tuple(errors),
            sources=tuple(answered),
        )

    def _attempt(
        self,
        name: str,
        adapter: QuoteAdapter,
        market: str,
        codes: list[str],
    ) -> _Attempt:
        quotes: list[Quote] = []
        skipped: list[str] = []
        errors: list[str] = []
        for start in range(0, len(codes), self.batch):
            chunk = codes[start : start + self.batch]
            if self.bucket is not None:
                self.bucket.acquire()
            try:
                batch = adapter.quotes(market, chunk)
            except Exception as exc:  # noqa: BLE001 — 一个源的坏实现不该带走整条链
                logger.warning("%s 报价失败：%s", name, exc)
                return _Attempt(
                    tuple(quotes),
                    tuple(skipped),
                    tuple(errors),
                    failure=f"{type(exc).__name__}: {exc}",
                )
            quotes.extend(batch.quotes)
            skipped.extend(batch.skipped)
            errors.extend(batch.errors)
        return _Attempt(tuple(quotes), tuple(skipped), tuple(errors))

    def _persist(self, market: str, quotes: list[Quote], moment: datetime) -> None:
        try:
            call_with_backoff(
                lambda: self.quotes.save(quotes),
                attempts=4,
                base=0.01,
                factor=5.0,
                sleep=self.sleep,
                retryable=(StorageError,),
                label=f"写入 {market} 报价快照",
            )
        except SamsError as exc:
            # 落库失败不改变这一轮的结论：下一轮会重写同一只票，板子仍然服务页面
            logger.error("%s 报价快照落库失败：%s", market, exc)

    def _record_source(
        self,
        name: str,
        adapter: QuoteAdapter,
        *,
        ok: bool,
        error: str | None,
        moment: datetime,
    ) -> None:
        if ok:
            self._failures[name] = 0
            self._last_ok[name] = moment
        else:
            self._failures[name] = self._failures.get(name, 0) + 1
        record = QuoteSourceRecord(
            name=name,
            markets=tuple(sorted(getattr(adapter, "markets", ()))),
            ok=ok,
            last_error=None if ok else error,
            last_ok_at=self._last_ok.get(name),
            consecutive_failures=self._failures.get(name, 0),
        )
        try:
            self.health.save(record)
        except SamsError as exc:
            logger.warning("报价源健康落库失败（%s）：%s", name, exc)

    def _note_outage(self, market: str, moment: datetime, *, got: bool) -> None:
        if got:
            self._outage_since.pop(market, None)
            self._alarmed.discard(market)
            return
        since = self._outage_since.setdefault(market, moment)
        if market in self._alarmed:
            return
        if (moment - since).total_seconds() < self.alarm_after_s:
            return
        self._alarmed.add(market)
        if self.bus is not None:
            self.bus.publish(
                SystemAlarm(
                    component=f"quote.{market}",
                    message=(
                        f"{market} 市场连续 {self.alarm_after_s // 60} 分钟没有取得任何报价；"
                        "检查报价源与网络"
                    ),
                )
            )
