"""盘中节拍器：只在常规时段问价、问该问的票、坏一轮不带走整个循环。"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from support import FrozenClock

from sams.core.errors import TransientError
from sams.services.intraday import IntradayMonitor
from sams.services.quotes import QuoteBoard, QuotePollResult

T0 = datetime(2026, 10, 23, 2, 0, tzinfo=UTC)


class FakeUniverse:
    def __init__(self, by_market: dict[str, tuple[str, ...]]) -> None:
        self.by_market = by_market
        self.calls = 0

    def build(self):
        self.calls += 1
        by_market = self.by_market

        class View:
            def codes(self, market: str) -> tuple[str, ...]:
                return by_market.get(market, ())

        return View()


class FlakyUniverse(FakeUniverse):
    def build(self):
        self.calls += 1
        if self.calls == 1:
            raise TransientError("universe", "ledger read failed")
        return super().build()


class FakeCalendar:
    def __init__(self, *, trading=None, open_at=None) -> None:
        self.trading = trading or {}
        self.open_at = open_at or {}

    def is_trading_day(self, market: str, day) -> bool:
        return self.trading.get(market, True)

    def is_market_open(self, market: str, when) -> bool:
        return self.open_at.get(market, True)


class FakeQuotes:
    def __init__(self) -> None:
        self.board = QuoteBoard()
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.fail: dict[str, Exception] = {}

    def poll(self, market, codes, *, now=None) -> QuotePollResult:
        self.calls.append((market, tuple(codes)))
        if market in self.fail:
            raise self.fail[market]
        return QuotePollResult(sources=("p",))


def monitor(settings, *, quotes=None, calendar=None, universe=None):
    return IntradayMonitor(
        settings,
        quotes or FakeQuotes(),
        calendar or FakeCalendar(),
        universe or FakeUniverse({"A": ("600519",), "HK": ("00700",), "US": ("AAPL",)}),
        clock=FrozenClock(T0),
        sleep=lambda _seconds: None,
    )


def test_only_open_markets_are_polled_with_their_own_codes(settings):
    quotes = FakeQuotes()
    minutes = monitor(
        settings,
        quotes=quotes,
        calendar=FakeCalendar(trading={"US": False}, open_at={"HK": False}),
    )

    outcomes = minutes.poll_once(now=T0)

    assert quotes.calls == [("A", ("600519",))]
    skipped = {outcome.market: outcome.skipped for outcome in outcomes}
    assert skipped["US"] == "非交易日"
    assert "非交易时段" in skipped["HK"]
    assert skipped["A"] is None


def test_a_market_with_nothing_to_watch_is_not_polled(settings):
    quotes = FakeQuotes()
    minutes = monitor(settings, quotes=quotes, universe=FakeUniverse({"A": ()}))
    outcomes = minutes.poll_once(now=T0)
    assert quotes.calls == []
    assert outcomes[0].skipped == "没有需要监控的标的"
    assert quotes.board.snapshot()["A"].skip_reason == "没有需要监控的标的"


def test_one_markets_failure_does_not_stop_the_next(settings):
    quotes = FakeQuotes()
    quotes.fail["A"] = TransientError("p", "boom")
    minutes = monitor(settings, quotes=quotes, calendar=FakeCalendar())

    outcomes = minutes.poll_once(now=T0)

    assert [call[0] for call in quotes.calls] == ["A", "HK", "US"]
    assert [outcome.market for outcome in outcomes] == ["A", "HK", "US"]
    assert outcomes[0].error and "boom" in outcomes[0].error
    assert quotes.board.snapshot()["A"].last_error


def test_a_failed_round_is_logged_and_the_loop_continues(settings):
    universe = FlakyUniverse({"A": ("600519",), "HK": (), "US": ()})
    quotes = FakeQuotes()
    minutes = monitor(settings, quotes=quotes, universe=universe)
    rounds = []

    count = minutes.run_forever(max_rounds=2, on_round=rounds.append)

    assert count == 2
    assert len(rounds) == 2 and rounds[0] == ()
    assert quotes.calls and quotes.calls[0][0] == "A"


def test_stop_event_ends_the_loop_before_the_next_round(settings):
    stop = threading.Event()
    rounds = []
    minutes = monitor(settings)

    count = minutes.run_forever(
        stop=stop, on_round=lambda outcomes: (rounds.append(outcomes), stop.set())
    )

    assert count == 1 and len(rounds) == 1


def test_poll_once_is_a_noop_for_closed_markets(settings):
    quotes = FakeQuotes()
    minutes = monitor(
        settings,
        quotes=quotes,
        calendar=FakeCalendar(trading={"A": False, "HK": False, "US": False}),
    )
    minutes.poll_once(now=T0)
    assert quotes.calls == []
    assert set(quotes.board.snapshot()) == {"A", "HK", "US"}
