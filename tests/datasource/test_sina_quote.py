from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from support import FrozenClock

from sams.core.errors import EmptyResponse
from sams.datasource.sina_quote import (
    QUOTE_URL,
    SinaQuoteAdapter,
    parse_sina_quotes,
    sina_quote_symbol,
)

NOW = datetime(2026, 10, 23, 14, 0, 5, tzinfo=UTC)


def line(key: str, cells: list[str]) -> str:
    return f'var hq_str_{key}="' + ",".join(cells) + '";'


def a_cells():
    cells = [""] * 33
    cells[0] = "贵州茅台"
    cells[1] = "1525.00"   # open
    cells[2] = "1530.00"   # prev close
    cells[3] = "1540.00"   # price
    cells[4] = "1545.00"   # high
    cells[5] = "1520.00"   # low
    cells[8] = "2400000"   # volume, shares
    cells[9] = "3612345678"
    cells[11] = "1539.90"
    cells[21] = "1540.10"
    cells[30] = "2026-10-23"
    cells[31] = "10:00:00"
    return cells


def test_symbols_for_three_markets():
    assert sina_quote_symbol("A", "600519") == "sh600519"
    assert sina_quote_symbol("HK", "700") == "hk00700"
    assert sina_quote_symbol("US", "AAPL.OQ") == "gb_aapl"


def test_a_share_fields_and_derived_pct():
    text = line("sh600519", a_cells())
    quote = parse_sina_quotes(
        text, market="A", codes=["600519"], now=NOW,
        zone=ZoneInfo("Asia/Shanghai"),
    ).quotes[0]
    assert quote.price == 1540.0 and quote.prev_close == 1530.0
    assert quote.open == 1525.0 and quote.high == 1545.0 and quote.low == 1520.0
    assert quote.volume == 2400000 and quote.amount == 3612345678
    assert quote.bid1 == 1539.9 and quote.ask1 == 1540.1
    assert quote.pct_chg == pytest.approx((1540 / 1530 - 1) * 100)
    assert quote.ts == datetime(2026, 10, 23, 2, 0, tzinfo=UTC)
    assert quote.source == "sina_quote"


def test_hk_fields_use_chinese_name_and_hk_time():
    cells = [""] * 19
    cells[0] = "TENCENT"
    cells[1] = "腾讯控股"
    cells[2] = "370.00"
    cells[3] = "368.00"
    cells[4] = "382.00"
    cells[5] = "369.00"
    cells[6] = "380.00"
    cells[7] = "12.00"
    cells[8] = "3.26"
    cells[9] = "379.80"
    cells[10] = "380.20"
    cells[11] = "380000000"
    cells[12] = "1000000"
    cells[17] = "2026/10/23"
    cells[18] = "10:00"
    quote = parse_sina_quotes(
        line("hk00700", cells), market="HK", codes=["00700"], now=NOW,
        zone=ZoneInfo("Asia/Hong_Kong"),
    ).quotes[0]
    assert quote.price == 380.0 and quote.pct_chg == 3.26
    assert quote.ts == datetime(2026, 10, 23, 2, 0, tzinfo=UTC)
    assert quote.volume == 1000000 and quote.amount == 380000000


def test_us_fields_use_price_datetime_and_prev_close_columns():
    cells = [""] * 31
    cells[0] = "Apple Inc"
    cells[1] = "230.50"
    cells[2] = "1.25"
    cells[3] = "2026-10-23 10:00:00"
    cells[4] = "2.85"
    cells[5] = "228.00"
    cells[6] = "231.00"
    cells[7] = "227.50"
    cells[10] = "50000000"
    cells[26] = "227.65"
    cells[30] = "11525000000"
    quote = parse_sina_quotes(
        line("gb_aapl", cells), market="US", codes=["AAPL"], now=NOW,
        zone=ZoneInfo("America/New_York"),
    ).quotes[0]
    assert quote.price == 230.5 and quote.prev_close == 227.65
    assert quote.ts == datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
    assert quote.volume == 50000000 and quote.amount == 11525000000


def test_an_empty_line_is_skipped_not_a_zero_quote():
    text = 'var hq_str_sh600519="";'
    batch = parse_sina_quotes(
        text, market="A", codes=["600519"], now=NOW,
        zone=ZoneInfo("Asia/Shanghai"),
    )
    assert batch.quotes == () and batch.skipped == ("600519",)


def test_echoed_key_must_match_the_requested_code():
    text = line("sh600518", a_cells())
    batch = parse_sina_quotes(
        text, market="A", codes=["600519"], now=NOW,
        zone=ZoneInfo("Asia/Shanghai"),
    )
    assert batch.quotes == () and batch.skipped == ("600519",)


def test_a_zero_price_is_an_error():
    cells = a_cells()
    cells[3] = "0.00"
    text = line("sh600519", cells)
    batch = parse_sina_quotes(
        text, market="A", codes=["600519"], now=NOW,
        zone=ZoneInfo("Asia/Shanghai"),
    )
    assert batch.quotes == () and batch.errors and "价格" in batch.errors[0]


def test_no_assignments_at_all_is_empty_response():
    with pytest.raises(EmptyResponse):
        parse_sina_quotes(
            "<html>403</html>", market="A", codes=["600519"], now=NOW,
            zone=ZoneInfo("Asia/Shanghai"),
        )


def test_adapter_uses_gbk_endpoint_and_its_own_name():
    seen = {}

    def fetch(url, params, *, timeout):
        seen.update(url=url, params=params, timeout=timeout)
        return line("sh600519", a_cells())

    adapter = SinaQuoteAdapter(
        quote_fetch=fetch, timeout=4, clock=FrozenClock(NOW)
    )
    batch = adapter.quotes("A", ["600519"])
    assert seen == {
        "url": QUOTE_URL,
        "params": {"list": "sh600519"},
        "timeout": 4,
    }
    assert batch.quotes[0].source == "sina_quote"


@pytest.mark.live
def test_live_sina_quote_smoke():
    batch = SinaQuoteAdapter().quotes("A", ["600519"])
    assert batch.quotes and batch.quotes[0].price > 0
