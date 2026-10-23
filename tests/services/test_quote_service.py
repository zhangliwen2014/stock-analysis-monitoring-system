"""取价：降级、部分返回、批次大小、落库重试与三分钟告警。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from support import FrozenClock

from sams.core.errors import StorageError, TransientError
from sams.core.events import EventBus, QuoteUpdated, SystemAlarm
from sams.core.quotes import Quote, QuoteBatch
from sams.datasource.registry import AdapterRegistry
from sams.services.quotes import QUOTE_OUTAGE_ALARM_S, QuoteBoard, QuoteService
from sams.storage.repos.quotes import QuoteHealthRepo, QuoteRepo

T0 = datetime(2026, 10, 23, 2, 0, tzinfo=UTC)
T1 = T0 + timedelta(seconds=10)


class StubQuoteAdapter:
    def __init__(self, name: str, behaviour, *, markets=("A",)) -> None:
        self.name = name
        self.markets = frozenset(markets)
        self.behaviour = behaviour
        self.calls: list[list[str]] = []

    def quotes(self, market: str, codes: list[str]) -> QuoteBatch:
        self.calls.append(list(codes))
        if isinstance(self.behaviour, Exception):
            raise self.behaviour
        return self.behaviour(market, codes)


def quote(code="600519", *, market="A", ts=T0, price=100.0, source="p") -> Quote:
    return Quote(market=market, code=code, ts=ts, price=price, source=source)


def batch_for(market: str, codes: list[str], *, source="p") -> QuoteBatch:
    return QuoteBatch(tuple(quote(code, market=market, source=source) for code in codes))


def make_service(
    db,
    adapters: dict[str, StubQuoteAdapter],
    *,
    market_sources: dict[str, tuple[str, ...]] | None = None,
    bus: EventBus | None = None,
    bucket=None,
    quotes_repo=None,
    sleep=lambda _seconds: None,
):
    registry = AdapterRegistry(
        adapters=adapters,
        market_sources=market_sources or {"A": tuple(adapters)},
    )
    board = QuoteBoard()
    service = QuoteService(
        registry,
        board,
        quotes_repo or QuoteRepo(db),
        QuoteHealthRepo(db),
        clock=FrozenClock(T0),
        bucket=bucket,
        bus=bus,
        sleep=sleep,
    )
    return service, board


def test_poll_persists_updates_board_records_health_and_publishes_events(db):
    events: list[QuoteUpdated] = []
    bus = EventBus()
    bus.subscribe(QuoteUpdated, events.append)
    primary = StubQuoteAdapter("p", batch_for)
    service, board = make_service(db, {"p": primary}, bus=bus)

    result = service.poll("A", ["600519"], now=T0)

    assert [q.code for q in result.quotes] == ["600519"]
    assert result.sources == ("p",)
    assert QuoteRepo(db).latest()[0].price == 100.0
    assert board.get("A", "600519").price == 100.0
    assert [(event.market, event.code, event.ts) for event in events] == [("A", "600519", T0)]
    record = QuoteHealthRepo(db).load()["p"]
    assert record.ok is True and record.last_ok_at == T0
    assert record.consecutive_failures == 0


def test_primary_failure_falls_back_and_both_sources_are_recorded(db):
    primary = StubQuoteAdapter("p", TransientError("p", "timeout"))
    backup = StubQuoteAdapter("s", lambda market, codes: batch_for(market, codes, source="s"))
    service, board = make_service(
        db, {"p": primary, "s": backup}, market_sources={"A": ("p", "s")}
    )

    result = service.poll("A", ["600519"], now=T0)

    assert [q.source for q in result.quotes] == ["s"]
    assert primary.calls == [["600519"]] and backup.calls == [["600519"]]
    health = QuoteHealthRepo(db).load()
    assert health["p"].ok is False and health["p"].consecutive_failures == 1
    assert health["s"].ok is True
    assert "p" in (board.snapshot()["A"].last_error or ""), "降级要留在板上，不能只写日志"


def test_a_second_failure_accumulates_and_success_resets(db):
    primary = StubQuoteAdapter("p", TransientError("p", "timeout"))
    service, _ = make_service(db, {"p": primary}, market_sources={"A": ("p",)})
    service.poll("A", ["600519"], now=T0)
    service.poll("A", ["600519"], now=T1)
    assert QuoteHealthRepo(db).load()["p"].consecutive_failures == 2

    primary.behaviour = batch_for
    service.poll("A", ["600519"], now=T1)
    record = QuoteHealthRepo(db).load()["p"]
    assert record.ok is True and record.consecutive_failures == 0


def test_skipped_codes_do_not_fall_back_and_are_never_written_as_zero(db):
    primary = StubQuoteAdapter(
        "p", lambda market, codes: QuoteBatch((quote(codes[0]),), tuple(codes[1:]))
    )
    backup = StubQuoteAdapter("s", batch_for)
    service, board = make_service(
        db, {"p": primary, "s": backup}, market_sources={"A": ("p", "s")}
    )

    result = service.poll("A", ["600519", "000001"], now=T0)

    assert [q.code for q in result.quotes] == ["600519"]
    assert result.skipped == ("000001",)
    assert backup.calls == [], "源正常回答时，跳过的代码是下一轮的事，不是换源的理由"
    assert [q.code for q in QuoteRepo(db).latest()] == ["600519"]
    assert board.get("A", "000001") is None


def test_a_large_universe_is_split_into_batches_of_fifty(db):
    primary = StubQuoteAdapter("p", batch_for)
    service, _ = make_service(db, {"p": primary})
    codes = [f"{i:06d}" for i in range(120)]

    result = service.poll("A", codes, now=T0)

    assert [len(call) for call in primary.calls] == [50, 50, 20]
    assert len(result.quotes) == 120


def test_an_empty_code_list_touches_nothing(db):
    primary = StubQuoteAdapter("p", batch_for)
    service, _ = make_service(db, {"p": primary})
    result = service.poll("A", [], now=T0)
    assert result.quotes == () and primary.calls == []


def test_a_market_without_a_quote_source_is_an_error_on_the_board(db):
    service, board = make_service(db, {}, market_sources={"A": ()})
    result = service.poll("A", ["600519"], now=T0)
    assert result.quotes == () and result.errors
    assert "报价源" in (board.snapshot()["A"].last_error or "")


def test_outage_marks_the_board_stale_and_alarms_exactly_once_after_three_minutes(db):
    alarms: list[SystemAlarm] = []
    bus = EventBus()
    bus.subscribe(SystemAlarm, alarms.append)
    primary = StubQuoteAdapter("p", batch_for)
    service, board = make_service(db, {"p": primary}, bus=bus)
    service.poll("A", ["600519"], now=T0)

    primary.behaviour = TransientError("p", "down")
    service.poll("A", ["600519"], now=T1)
    assert alarms == [], "第一次失败不告警：一次超时不是事故"

    service.poll("A", ["600519"], now=T1 + timedelta(seconds=QUOTE_OUTAGE_ALARM_S - 1))
    assert alarms == []

    service.poll("A", ["600519"], now=T1 + timedelta(seconds=QUOTE_OUTAGE_ALARM_S))
    service.poll("A", ["600519"], now=T1 + timedelta(seconds=QUOTE_OUTAGE_ALARM_S + 60))

    assert len(alarms) == 1
    assert alarms[0].component == "quote.A"
    assert board.snapshot()["A"].quotes["600519"].is_stale is True


def test_recovery_clears_the_outage_so_a_later_outage_can_alarm_again(db):
    alarms: list[SystemAlarm] = []
    bus = EventBus()
    bus.subscribe(SystemAlarm, alarms.append)
    primary = StubQuoteAdapter("p", TransientError("p", "down"))
    service, board = make_service(db, {"p": primary}, bus=bus)
    service.poll("A", ["600519"], now=T0)
    service.poll("A", ["600519"], now=T0 + timedelta(seconds=QUOTE_OUTAGE_ALARM_S))
    assert len(alarms) == 1

    primary.behaviour = batch_for
    service.poll("A", ["600519"], now=T0 + timedelta(seconds=QUOTE_OUTAGE_ALARM_S + 10))
    assert board.snapshot()["A"].quotes["600519"].is_stale is False

    primary.behaviour = TransientError("p", "down again")
    start = T0 + timedelta(seconds=QUOTE_OUTAGE_ALARM_S + 20)
    service.poll("A", ["600519"], now=start)
    service.poll("A", ["600519"], now=start + timedelta(seconds=QUOTE_OUTAGE_ALARM_S))
    assert len(alarms) == 2


def test_snapshot_write_is_retried_without_losing_the_board(db):
    class FlakyRepo(QuoteRepo):
        def __init__(self, database):
            super().__init__(database)
            self.calls = 0

        def save(self, quotes):
            self.calls += 1
            if self.calls < 3:
                raise StorageError("another connection is writing")
            return super().save(quotes)

    repo = FlakyRepo(db)
    primary = StubQuoteAdapter("p", batch_for)
    service, board = make_service(db, {"p": primary}, quotes_repo=repo)

    result = service.poll("A", ["600519"], now=T0)

    assert repo.calls == 3
    assert result.quotes and board.get("A", "600519") is not None
    assert len(QuoteRepo(db).latest()) == 1


def test_a_permanently_failing_snapshot_write_does_not_kill_the_poll(db):
    class BrokenRepo:
        def save(self, quotes):
            raise StorageError("schema drift")

    primary = StubQuoteAdapter("p", batch_for)
    service, board = make_service(db, {"p": primary}, quotes_repo=BrokenRepo())

    result = service.poll("A", ["600519"], now=T0)

    assert result.quotes, "这一轮的价格仍然是事实"
    assert board.get("A", "600519") is not None, "板子先服务页面，落库下一轮再试"


def test_an_all_skipped_round_is_neither_stale_nor_an_outage(db):
    alarms: list[SystemAlarm] = []
    bus = EventBus()
    bus.subscribe(SystemAlarm, alarms.append)
    primary = StubQuoteAdapter("p", batch_for)
    service, board = make_service(db, {"p": primary}, bus=bus)
    service.poll("A", ["600519"], now=T0)

    primary.behaviour = lambda market, codes: QuoteBatch((), tuple(codes))
    late = T0 + timedelta(seconds=QUOTE_OUTAGE_ALARM_S)
    service.poll("A", ["600519"], now=late)
    service.poll("A", ["600519"], now=late + timedelta(seconds=60))
    service.poll("A", ["600519"], now=late + timedelta(seconds=180))

    assert alarms == [], "源明确回答了：跳过不是事故，不该响起「检查报价源与网络」"
    view = board.snapshot()["A"]
    assert view.quotes["600519"].is_stale is False
    assert view.skip_reason is not None


def test_a_failing_source_still_reports_what_it_skipped(db):
    class TwoPhase:
        def __init__(self):
            self.calls = 0

        def __call__(self, market, codes):
            self.calls += 1
            if self.calls == 1:
                return QuoteBatch((), (codes[0],))
            raise TransientError("p", "boom")

    primary = StubQuoteAdapter("p", TwoPhase())
    backup = StubQuoteAdapter("s", batch_for)
    registry = AdapterRegistry(
        adapters={"p": primary, "s": backup}, market_sources={"A": ("p", "s")}
    )
    service = QuoteService(
        registry,
        QuoteBoard(),
        QuoteRepo(db),
        QuoteHealthRepo(db),
        clock=FrozenClock(T0),
        batch=1,
        sleep=lambda _s: None,
    )

    result = service.poll("A", ["600519", "000001"], now=T0)

    assert result.skipped == ("600519",)
    assert [q.code for q in result.quotes] == ["000001"]
    assert result.sources == ("s",)


def test_each_batch_acquires_from_the_bucket(db):
    class CountingBucket:
        def __init__(self):
            self.acquires = 0

        def acquire(self, tokens: float = 1.0) -> float:
            self.acquires += 1
            return 0.0

    bucket = CountingBucket()
    primary = StubQuoteAdapter("p", batch_for)
    registry = AdapterRegistry(adapters={"p": primary}, market_sources={"A": ("p",)})
    service = QuoteService(
        registry,
        QuoteBoard(),
        QuoteRepo(db),
        QuoteHealthRepo(db),
        clock=FrozenClock(T0),
        bucket=bucket,
        sleep=lambda _s: None,
    )

    service.poll("A", [f"{i:06d}" for i in range(120)], now=T0)

    assert bucket.acquires == 3
