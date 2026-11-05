from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from support import FrozenClock

from sams.core.errors import EmptyResponse, RateLimited
from sams.datasource.sina_quote import (
    QUOTE_URL,
    SinaQuoteAdapter,
    parse_sina_quotes,
    quote_get,
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


def test_us_class_shares_have_no_sina_key_so_the_symbol_refuses():
    # `.OQ` 是交易所后缀，剥掉后仍是同一只票；`.B`/`.A` 是类股，新浪的键表达不了
    # （实测 gb_brk_b / gb_brk_a 都是空行，gb_brkb 是 2019 年的死行）。按点前裸代码
    # 发 gb_brk 会拿到 A 类的报价且毫无异常——键就是回显，只能在这一步拒绝。
    assert sina_quote_symbol("US", "AAPL.OQ") == "gb_aapl"
    with pytest.raises(ValueError):
        sina_quote_symbol("US", "BRK.B")
    with pytest.raises(ValueError):
        sina_quote_symbol("US", "BRK.A")


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


def hk_cells():
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
    return cells


def test_hk_fields_use_chinese_name_and_hk_time():
    quote = parse_sina_quotes(
        line("hk00700", hk_cells()), market="HK", codes=["00700"], now=NOW,
        zone=ZoneInfo("Asia/Hong_Kong"),
    ).quotes[0]
    assert quote.price == 380.0 and quote.pct_chg == 3.26
    assert quote.ts == datetime(2026, 10, 23, 2, 0, tzinfo=UTC)
    assert quote.volume == 1000000 and quote.amount == 380000000


def test_the_hk_row_declares_the_delay_this_feed_is_sold_at():
    """新浪的港股报价牌上就写着延迟：11-05 实测落后墙钟约 28 分钟，取 1800 秒。

    比腾讯那一路还慢一倍，所以这个数只能跟着报价走、不能按市场记一个常数（D319）
    """
    quote = parse_sina_quotes(
        line("hk00700", hk_cells()), market="HK", codes=["00700"], now=NOW,
        zone=ZoneInfo("Asia/Hong_Kong"),
    ).quotes[0]
    assert quote.delayed is True
    assert quote.delay_s == 1800

    a = parse_sina_quotes(
        line("sh600519", a_cells()), market="A", codes=["600519"], now=NOW,
        zone=ZoneInfo("Asia/Shanghai"),
    ).quotes[0]
    assert a.delayed is None
    assert a.delay_s == 0


def test_the_us_row_declares_no_delay_because_it_is_not_delayed():
    """#386 实测（11-06 盘中两次，北京 02:39 / 02:52）：新浪美股那一格落后墙钟 1.5~2 秒，
    腾讯 5~7 秒——两路都是实时源，没有可声明的固定延时（D319）。

    这里钉的是「没测过就别声明」的另一半：测过了、结果是实时，就不许再挂一个数。
    """
    quote = parse_sina_quotes(
        line("gb_aapl", us_cells()), market="US", codes=["AAPL"], now=NOW,
        zone=ZoneInfo("America/New_York"),
    ).quotes[0]
    assert quote.delayed is None
    assert quote.delay_s == 0


def us_cells():
    """美股那一行：`2026-11-06 02:39:05` 是**北京时间**，不是纽约时间。

    11-06 实机对表：取回时墙钟北京时间 02:39:06，源给的这一格 02:39:05；
    纽约当时是 11-05 13:39。按纽约解析会把这根报价挪到未来 13 小时。
    """
    cells = [""] * 31
    cells[0] = "Apple Inc"
    cells[1] = "230.50"
    cells[2] = "1.25"
    cells[3] = "2026-10-23 22:00:00"
    cells[4] = "2.85"
    cells[5] = "228.00"
    cells[6] = "231.00"
    cells[7] = "227.50"
    cells[10] = "50000000"
    cells[26] = "227.65"
    cells[30] = "11525000000"
    return cells


def test_us_fields_use_price_datetime_and_prev_close_columns():
    quote = parse_sina_quotes(
        line("gb_aapl", us_cells()), market="US", codes=["AAPL"], now=NOW,
        zone=ZoneInfo("America/New_York"),
    ).quotes[0]
    assert quote.price == 230.5 and quote.prev_close == 227.65
    # 北京 22:00 = 纽约 10:00（EDT）——同一时刻，一个写成北京时间、一个写成纽约时间
    assert quote.ts == datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
    assert quote.volume == 50000000 and quote.amount == 11525000000


def test_the_us_row_is_read_as_beijing_time_not_new_york():
    """把北京那一格当纽约读，报价会落到 13 小时后的未来，新鲜度闸就废了。

    同一时刻：北京 2026-10-23 22:00 = 纽约 10:00（EDT）= UTC 14:00；
    按纽约读会得到次日 02:00 UTC，落在 NOW 之后。
    """
    quote = parse_sina_quotes(
        line("gb_aapl", us_cells()), market="US", codes=["AAPL"], now=NOW,
        zone=ZoneInfo("America/New_York"),
    ).quotes[0]
    assert quote.ts == datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
    assert quote.ts <= NOW


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


def test_a_us_class_share_is_skipped_not_served_the_class_a_price():
    seen = {}

    def fetch(url, params, *, timeout):
        seen.update(params=params)
        return line("gb_aapl", us_cells())

    adapter = SinaQuoteAdapter(quote_fetch=fetch, clock=FrozenClock(NOW))
    batch = adapter.quotes("US", ["AAPL", "BRK.B"])
    assert [q.code for q in batch.quotes] == ["AAPL"]
    # 这只票在这个源拿不到键，宁可出现在 skipped 也不能拿别的类股的价格
    assert batch.skipped == ("BRK.B",)
    # 请求清单里不得出现 gb_brk——那是伯克希尔 A 类的键
    assert seen["params"]["list"] == "gb_aapl"


def test_a_batch_of_only_class_shares_never_hits_the_endpoint():
    seen = {}

    def fetch(url, params, *, timeout):
        seen.update(url=url)
        return ""

    adapter = SinaQuoteAdapter(quote_fetch=fetch, clock=FrozenClock(NOW))
    batch = adapter.quotes("US", ["BRK.B"])
    assert batch.skipped == ("BRK.B",)
    assert seen == {}


def test_the_adapter_skips_index_codes_without_requesting_them():
    # 新浪报价不覆盖指数（D145）：指数码直接进 skipped，不进请求、不记 errors
    seen = {}

    def fetch(url, params, *, timeout):
        seen.update(params=params)
        return line("sh600519", a_cells())

    adapter = SinaQuoteAdapter(quote_fetch=fetch, clock=FrozenClock(NOW))
    batch = adapter.quotes("A", ["600519", "sh000001"])
    assert seen["params"] == {"list": "sh600519"}
    assert [q.code for q in batch.quotes] == ["600519"]
    assert batch.skipped == ("sh000001",)
    assert batch.errors == ()


def test_quote_get_maps_429_to_rate_limited(monkeypatch):
    # 429 必须在 raise_for_status 之前判成 RateLimited：路由器对限流退避重试，
    # 对其他异常立刻换源——限流时换源等于把备源也打爆
    class Response429:
        status_code = 429

    monkeypatch.setattr("requests.get", lambda *a, **k: Response429())
    with pytest.raises(RateLimited):
        quote_get(QUOTE_URL, {"list": "sh600519"}, timeout=1)


def test_the_batch_comma_reaches_sina_unencoded(monkeypatch):
    """新浪按字面量回键：逗号被编码成 `%2C` 时，整批只回一行空壳。

    11-06 实机抓到的原样：请求 `?list=gb_aapl,gb_nvda`，若走 requests 的 params
    就变成 `?list=gb_aapl%2Cgb_nvda`，响应是
    `var hq_str_gb_aapl%2Cgb_nvda="";`——每只票都静默 skipped，备源等于没接上。
    A/HK/US 三个市场一样；腾讯那边同形的 `q=` 会解 %2C，所以只有这一路要自己拼。
    """
    seen = {}

    class Response:
        status_code = 200
        content = b'var hq_str_sh600519="";\n'

        def raise_for_status(self):
            pass

    def fake_get(url, **_kwargs):
        seen["url"] = url
        return Response()

    monkeypatch.setattr("requests.get", fake_get)
    quote_get(QUOTE_URL, {"list": "sh600519,sz000001"}, timeout=1)
    assert seen["url"] == f"{QUOTE_URL}?list=sh600519,sz000001"


@pytest.mark.live
def test_live_sina_quote_smoke():
    batch = SinaQuoteAdapter().quotes("A", ["600519"])
    assert batch.quotes and batch.quotes[0].price > 0


@pytest.mark.live
def test_live_sina_quote_hk_and_us_smoke():
    hk = SinaQuoteAdapter().quotes("HK", ["00700"])
    assert hk.quotes and hk.quotes[0].price > 0
    assert hk.quotes[0].ts.tzinfo is not None
    us = SinaQuoteAdapter().quotes("US", ["AAPL"])
    assert us.quotes and us.quotes[0].price > 0
    assert us.quotes[0].ts.tzinfo is not None
