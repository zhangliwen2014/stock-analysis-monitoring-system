"""QuoteBoard.previous：被换下来的那一轮是「穿」的另一侧（§5.2）。"""

from __future__ import annotations

from datetime import UTC, datetime

from sams.core.quotes import Quote
from sams.services.quotes import QuoteBoard

NOW = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)


def quote(price, **over):
    base = {"market": "A", "code": "601088", "ts": NOW, "price": price}
    base.update(over)
    return Quote(**base)


def test_the_displaced_round_becomes_previous():
    board = QuoteBoard()
    board.update("A", (quote(9.95),), now=NOW)
    board.update("A", (quote(9.40),), now=NOW)
    previous = board.previous("A", "601088")
    assert previous is not None and previous.price == 9.95
    assert board.get("A", "601088").price == 9.40


def test_the_first_round_has_no_previous():
    board = QuoteBoard()
    board.update("A", (quote(9.4),), now=NOW)
    assert board.previous("A", "601088") is None


def test_seed_does_not_write_previous():
    # 开局灌的是日线最后收盘：那不是「上一轮」，把它当上一轮会让 9:30 的第一跳误判
    board = QuoteBoard()
    board.seed((quote(9.95),))
    board.update("A", (quote(9.40),), now=NOW)
    assert board.previous("A", "601088") is None


def test_fail_and_skip_do_not_write_previous():
    board = QuoteBoard()
    board.update("A", (quote(9.4),), now=NOW)
    board.update("A", (quote(9.3),), now=NOW)
    board.fail("A", "源挂了", now=NOW)
    board.skip("HK", "非交易时段", now=NOW)
    previous = board.previous("A", "601088")
    assert previous is not None and previous.price == 9.4


def test_previous_is_keyed_by_market_and_code():
    board = QuoteBoard()
    board.update("A", (quote(9.4), quote(199.0, market="HK", code="00700")), now=NOW)
    board.update("A", (quote(9.3), quote(198.0, market="HK", code="00700")), now=NOW)
    assert board.previous("A", "601088").price == 9.4
    assert board.previous("HK", "00700").price == 199.0
    assert board.previous("HK", "09988") is None
