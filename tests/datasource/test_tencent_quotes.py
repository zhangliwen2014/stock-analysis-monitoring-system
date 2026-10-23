from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from support import FrozenClock

from sams.datasource.tencent import (
    QUOTE_URL,
    TencentAdapter,
    parse_tencent_quotes,
    tencent_quote_symbol,
)

NOW = datetime(2026, 10, 23, 14, 0, 5, tzinfo=UTC)


def line(key: str, fields: list[str]) -> str:
    return f'v_{key}="{"~".join(fields)}";'


def fields(*, code: str, price="100.00", prev="99.00", open_="98.50",
           high="101.00", low="97.00", volume="12345", ts="",
           pct="1.01", bid="99.99", ask="100.01", amount="", amount_parts=None):
    cells = [""] * 40
    cells[0] = "1"
    cells[1] = "测试票"
    cells[2] = code
    cells[3] = price
    cells[4] = prev
    cells[5] = open_
    cells[6] = volume
    cells[9] = bid
    cells[19] = ask
    cells[30] = ts
    cells[31] = "1.00"
    cells[32] = pct
    cells[33] = high
    cells[34] = low
    cells[35] = amount_parts or amount
    cells[37] = amount
    return cells


def test_symbol_shapes_for_three_markets():
    assert tencent_quote_symbol("A", "600519") == "sh600519"
    assert tencent_quote_symbol("HK", "700") == "hk00700"
    # 报价接口不要交易所后缀：带 .OQ 会返回空行，看起来像「这只票没有报价」
    assert tencent_quote_symbol("US", "AAPL.OQ") == "usAAPL"


def test_a_share_quote_fields_timezone_and_lot_conversion():
    text = line("sh600519", fields(
        code="600519",
        price="1540.00",
        prev="1530.00",
        open_="1525.00",
        high="1545.00",
        low="1520.00",
        volume="23456",
        ts="20261023100000",
        pct="0.65",
        amount_parts="0/0/3612345678",
    ))
    batch = parse_tencent_quotes(
        text, market="A", codes=["600519"], now=NOW,
        zone=ZoneInfo("Asia/Shanghai"),
    )
    quote = batch.quotes[0]
    assert (quote.price, quote.prev_close, quote.open) == (1540.0, 1530.0, 1525.0)
    assert quote.high == 1545.0 and quote.low == 1520.0
    # 腾讯 A 股给「手」，入库统一为「股」
    assert quote.volume == 23456 * 100
    assert quote.amount == 3612345678
    assert quote.pct_chg == 0.65
    assert quote.bid1 == 99.99 and quote.ask1 == 100.01
    assert quote.ts == datetime(2026, 10, 23, 2, 0, tzinfo=UTC)
    assert quote.source == "tencent" and quote.is_stale is False


def test_hk_quote_uses_hk_timestamp_and_amount_column():
    text = line("hk00700", fields(
        code="00700",
        price="380.00",
        volume="1000000",
        ts="2026/10/23 10:00:00",
        amount="380000000",
    ))
    quote = parse_tencent_quotes(
        text, market="HK", codes=["700"], now=NOW,
        zone=ZoneInfo("Asia/Hong_Kong"),
    ).quotes[0]
    assert quote.code == "00700"
    assert quote.ts == datetime(2026, 10, 23, 2, 0, tzinfo=UTC)
    assert quote.volume == 1000000 and quote.amount == 380000000


def test_us_quote_accepts_exchange_suffix_in_the_echo():
    text = line("usAAPL", fields(
        code="AAPL.OQ",
        price="230.50",
        ts="2026-10-23 10:00:00",
        amount="123456789",
    ))
    quote = parse_tencent_quotes(
        text, market="US", codes=["AAPL"], now=NOW,
        zone=ZoneInfo("America/New_York"),
    ).quotes[0]
    assert quote.code == "AAPL"
    assert quote.ts == datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
    assert quote.amount == 123456789


def test_missing_and_mismatched_codes_are_skipped_not_zero():
    text = line("sh600519", fields(code="600519", ts="20261023100000"))
    batch = parse_tencent_quotes(
        text, market="A", codes=["600519", "000001"], now=NOW,
        zone=ZoneInfo("Asia/Shanghai"),
    )
    assert [q.code for q in batch.quotes] == ["600519"]
    assert batch.skipped == ("000001",)
    assert batch.errors == ()


def test_a_quote_without_a_tradable_price_is_an_error_not_a_zero():
    text = line("sh600519", fields(code="600519", price="0", ts="20261023100000"))
    batch = parse_tencent_quotes(
        text, market="A", codes=["600519"], now=NOW,
        zone=ZoneInfo("Asia/Shanghai"),
    )
    assert batch.quotes == ()
    assert batch.errors and "价格" in batch.errors[0]


def test_an_unreadable_time_falls_back_to_fetch_time_and_is_marked_stale():
    text = line("sh600519", fields(code="600519", ts="not-a-time"))
    quote = parse_tencent_quotes(
        text, market="A", codes=["600519"], now=NOW,
        zone=ZoneInfo("Asia/Shanghai"),
    ).quotes[0]
    assert quote.ts == NOW and quote.is_stale is True


def test_empty_response_is_an_empty_response_not_an_empty_batch():
    from sams.core.errors import EmptyResponse

    with pytest.raises(EmptyResponse):
        parse_tencent_quotes(
            "", market="A", codes=["600519"], now=NOW,
            zone=ZoneInfo("Asia/Shanghai"),
        )


def test_the_adapter_posts_the_batch_to_the_quote_endpoint():
    seen = {}

    def fetch(url, params, *, timeout):
        seen.update(url=url, params=params, timeout=timeout)
        return line("sh600519", fields(code="600519", ts="20261023100000"))

    adapter = TencentAdapter(
        quote_fetch=fetch,
        timeout=7,
        clock=FrozenClock(NOW),
    )
    batch = adapter.quotes("A", ["600519"])
    assert seen["url"] == QUOTE_URL
    assert seen["params"] == {"q": "sh600519"}
    assert seen["timeout"] == 7
    assert batch.quotes[0].code == "600519"


@pytest.mark.live
def test_live_tencent_quote_smoke():
    batch = TencentAdapter().quotes("A", ["600519"])
    assert batch.quotes and batch.quotes[0].price > 0
    assert batch.quotes[0].ts.tzinfo is not None
