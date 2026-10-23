"""行情板：读方拿到的是副本，写方一轮只动一个市场。

盘中的读（页面）与写（轮询线程）在同一个进程里并发，锁不是可选项；
但锁之外还有一条契约：`snapshot()` 返回的字典改了不能影响板子本身。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sams.core.quotes import Quote
from sams.services.quotes import QuoteBoard

T0 = datetime(2026, 10, 23, 2, 0, tzinfo=UTC)


def quote(code="600519", *, market="A", price=100.0, ts=T0) -> Quote:
    return Quote(market=market, code=code, ts=ts, price=price, source="tencent")


def test_update_puts_quotes_in_place_and_clears_the_skip_reason():
    board = QuoteBoard()
    board.skip("A", "午休", now=T0)
    board.update("A", [quote()], now=T0)
    view = board.snapshot()["A"]
    assert view.quotes["600519"].price == 100.0
    assert view.last_poll_at == T0
    assert view.skip_reason is None and view.last_error is None


def test_failure_keeps_the_last_quote_but_marks_it_stale():
    board = QuoteBoard()
    board.update("A", [quote()], now=T0)
    board.fail("A", "全部源超时", now=T0)
    view = board.snapshot()["A"]
    assert view.quotes["600519"].is_stale is True
    assert view.quotes["600519"].price == 100.0, "失败不删除上一笔：页面要能显示最后已知价"
    assert view.last_error == "全部源超时"


def test_snapshot_is_a_copy():
    board = QuoteBoard()
    board.update("A", [quote()], now=T0)
    view = board.snapshot()
    view["A"].quotes.clear()
    view["A"].quotes["000001"] = quote("000001")
    assert set(board.snapshot()["A"].quotes) == {"600519"}


def test_a_skip_does_not_touch_the_quotes_even_across_a_session_break():
    """午休期间页面显示上午的最后一笔是正常的：`quote_state` 会把市场判成 closed，
    这里不额外标 stale——把闭市标成陈旧会让每个午休都像一次故障。
    """
    board = QuoteBoard()
    board.update("A", [quote()], now=T0)
    board.skip("A", "非交易时段（午休）", now=T0)
    view = board.snapshot()["A"]
    assert view.quotes["600519"].is_stale is False
    assert view.skip_reason == "非交易时段（午休）"
