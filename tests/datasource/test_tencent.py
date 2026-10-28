"""腾讯 gtimg 适配器：录制响应 → 标准列集，以及三市场各自的参数形状。

这个源是港美股唯一的备源，所以它「静默给错东西」的几种形态都要有用例钉住：
`qfqday`（复权口径混进 raw）、`code!=0`（参数错却看起来像没数据）、
美股不带后缀（返回空 day 数组）。这三种在旧实现里都会变成「今天就是没数据」。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest
from support import FrozenClock

from sams.core.errors import EmptyResponse, FieldDrift, RateLimited, TransientError
from sams.datasource.base import BAR_COLUMNS
from sams.datasource.minute import MINUTE_SERIES_COLUMNS
from sams.datasource.tencent import (
    TencentAdapter,
    _bars_for,
    kline_param,
    parse_tencent_daily,
    parse_tencent_minute,
    parse_us_hint,
    tencent_symbol,
)

START, END = date(2024, 1, 2), date(2024, 1, 5)
DAY = date(2026, 10, 28)

#: A 股：[日期, 开, 收, 高, 低, 量(手)]
A_PAYLOAD = {
    "code": 0,
    "data": {
        "sh600519": {
            "day": [
                ["2024-01-02", "1800.00", "1810.00", "1820.00", "1790.00", "30000"],
                ["2024-01-03", "1810.00", "1815.00", "1830.00", "1800.00", "28000"],
            ]
        }
    },
}

#: 港股：量是「股」，且行尾还挂着一个公司行为对象（以前它会让按位置取数崩掉）
HK_PAYLOAD = {
    "code": 0,
    "data": {
        "hk00700": {
            "day": [
                ["2024-01-02", "280.00", "282.00", "283.00", "279.00", "23354069", {"t": "0"}],
            ]
        }
    },
}


#: 美股：走 kline/kline，symbol 必须带交易所后缀
US_PAYLOAD = {
    "code": 0,
    "data": {"usAAPL.OQ": {"day": [["2024-01-02", "1", "2", "3", "4", "5"]]}},
}


def parse(payload=A_PAYLOAD, market="A", code="600519", symbol=None):
    return parse_tencent_daily(
        payload,
        market=market,
        code=code,
        symbol=symbol if symbol is not None else tencent_symbol(market, code),
    )


def test_recorded_a_share_payload_becomes_standard_bars():
    out = parse()
    assert out["date"].to_list() == [date(2024, 1, 2), date(2024, 1, 3)]
    # 收盘在**第三列**：取错一位就是把开盘价当收盘价，而数据看起来完全正常
    assert out["close"].to_list() == [1810.0, 1815.0]
    assert out["open"].to_list() == [1800.0, 1810.0]
    assert out["high"].to_list() == [1820.0, 1830.0]
    assert out["source"].unique().to_list() == ["tencent"]


def test_a_share_volume_is_lots_and_becomes_shares():
    assert parse()["volume"].to_list() == [3_000_000.0, 2_800_000.0]


def test_hk_volume_is_already_shares_and_trailing_object_is_ignored():
    out = parse(HK_PAYLOAD, market="HK", code="00700")
    assert out["volume"].to_list() == [23354069.0]
    assert out["close"].to_list() == [282.0]
    assert out["market"].unique().to_list() == ["HK"]


def test_amount_is_absent_not_zero():
    # 腾讯日线不给成交额：留 NULL 是「这个源没这个字段」，写 0 是「今天没人交易」
    assert parse()["amount"].to_list() == [None, None]


@pytest.mark.parametrize(
    "payload,expected",
    [
        # 空对象：分不清「接口坏了」与「这段没数据」，按「没有数据」处理并继续问下一个源
        ({}, EmptyResponse),
        ("[]", FieldDrift),
        ({"code": 1, "msg": "bad params"}, FieldDrift),
        ({"code": 0, "data": {}}, EmptyResponse),
        ({"code": 0, "data": {"sh600519": {"day": []}}}, EmptyResponse),
        ({"code": 0, "data": {"sh600519": "x"}}, FieldDrift),
    ],
)
def test_silently_wrong_responses_are_named_not_empty(payload, expected):
    with pytest.raises(expected):
        parse(payload)


def test_qfq_response_is_refused_because_the_store_only_takes_raw():
    day = ["2024-01-02", "1", "2", "3", "4", "5"]
    payload = {"code": 0, "data": {"sh600519": {"qfqday": [day]}}}
    with pytest.raises(FieldDrift, match="复权序列不入库"):
        parse(payload)


def test_short_row_is_field_drift_not_index_error():
    payload = {"code": 0, "data": {"sh600519": {"day": [["2024-01-02", "1", "2"]]}}}
    with pytest.raises(FieldDrift, match="字段不足"):
        parse(payload)


def test_a_response_for_another_symbol_is_refused_not_stored():
    """接口把别的票放进同一个节点时，那条序列字段齐全、日期连续、看起来完全正常。

    按 `next(iter(data.values()))` 取数会把它当成 600519 存进库，事后无从查出。
    """
    payload = {"code": 0, "data": {"sh600036": A_PAYLOAD["data"]["sh600519"]}}
    with pytest.raises(FieldDrift, match="响应的不是 sh600519"):
        parse(payload)


def test_symbol_mapping_per_market():
    assert tencent_symbol("A", "600519") == "sh600519"
    assert tencent_symbol("A", "000001") == "sz000001"
    assert tencent_symbol("HK", "700") == "hk00700"
    assert tencent_symbol("US", "usAAPL.OQ") == "usAAPL.OQ"
    with pytest.raises(ValueError):
        tencent_symbol("JP", "7203")


def test_only_a_share_kline_takes_the_trailing_adjust_slot():
    # 两个端点的参数形状不一样：给 kline/kline 多加一位会被拒，
    # 而 fqkline 少这一位会按默认口径返回复权价——两头都是静默错误
    assert kline_param("A", "sh600519", START, END, 8) == "sh600519,day,2024-01-02,2024-01-05,8,"
    assert kline_param("HK", "hk00700", START, END, 8) == "hk00700,day,2024-01-02,2024-01-05,8"
    assert _bars_for(START, END) == 8


def test_us_hint_picks_the_exact_ticker_not_the_first_candidate():
    text = 'v_hint="us~ba.n~波音~pg~GP^us~aapl.oq~苹果~pg~GP"'
    assert parse_us_hint(text, "AAPL") == "usAAPL.OQ"


def test_us_hint_rejects_candidates_that_only_share_a_prefix():
    # 查 B 时列表里是 b.n 与 ba-a.n：都不算「B」，宁可报找不到
    text = 'v_hint="us~b.n~巴里克~pg~GP^us~ba-a.n~波音B~pg~GP"'
    with pytest.raises(EmptyResponse, match="未找到美股"):
        parse_us_hint(text, "BB")


def test_us_hint_without_any_us_candidate_is_not_found():
    with pytest.raises(EmptyResponse):
        parse_us_hint('v_hint="cn~sh600519~贵州茅台"', "AAPL")


class FakeGet:
    """记录被调用的 url 与 param，并返回预置响应。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, params, *, timeout=15):
        self.calls.append((url, params["param"]))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def adapter_with(payload, *, resolve=None):
    get = FakeGet(payload)
    hint_calls = []

    def hint_get(url, params, *, timeout=15):
        hint_calls.append(params["q"])
        return 'v_hint="us~aapl.oq~苹果~pg~GP"'

    return (
        TencentAdapter(get=get, hint_get=hint_get, resolve=resolve),
        get,
        hint_calls,
    )


def test_a_share_request_targets_fqkline_with_empty_adjust():
    adapter, get, _ = adapter_with(A_PAYLOAD)
    adapter.daily_bars("A", "600519", START, END)
    url, param = get.calls[0]
    assert url.endswith("fqkline/get") and param.endswith(",")


def test_hk_and_us_request_targets_kline_and_carries_resolved_suffix():
    adapter, get, hint_calls = adapter_with(US_PAYLOAD)
    adapter.daily_bars("US", "AAPL", START, END)
    url, param = get.calls[0]
    assert url.endswith("kline/kline")
    assert param.startswith("usAAPL.OQ,day,")
    assert hint_calls == ["AAPL"]


def test_us_suffix_is_resolved_once_per_symbol():
    payload = {"code": 0, "data": {"usAAPL.OQ": {"day": [["2024-01-02", "1", "2", "3", "4", "5"]]}}}
    resolved = []

    def resolve(symbol):
        resolved.append(symbol)
        return "usAAPL.OQ"

    adapter, _, _ = adapter_with(payload, resolve=resolve)
    for _ in range(3):
        adapter.daily_bars("US", "AAPL", START, END)
    assert resolved == ["AAPL"], "一次回补不该为同一只票重复问 smartbox"


def test_unsupported_market_is_a_programming_error():
    adapter, _, _ = adapter_with(A_PAYLOAD)
    with pytest.raises(ValueError):
        adapter.daily_bars("UK", "A", START, END)


def test_reversed_range_is_refused_before_the_network():
    adapter, get, _ = adapter_with(A_PAYLOAD)
    with pytest.raises(ValueError, match="区间反向"):
        adapter.daily_bars("A", "600519", END, START)
    assert get.calls == []


@pytest.mark.parametrize(
    "boom,expected",
    [(ConnectionError("reset"), TransientError), (TimeoutError("slow"), TransientError)],
)
def test_transport_failures_are_classified(boom, expected):
    adapter, _, _ = adapter_with(boom)
    with pytest.raises(expected):
        adapter.daily_bars("A", "600519", START, END)


# --- 指数日线（D148）：同一个 kline 端点，符号不过 tencent_symbol ----------------------


#: 指数的响应节点键就是源形态的指数码
INDEX_A_PAYLOAD = {
    "code": 0,
    "data": {"sh000001": {"day": [["2024-01-02", "1", "2", "3", "4", "5"]]}},
}


def test_tencent_index_daily_a():
    get = FakeGet(INDEX_A_PAYLOAD)
    adapter = TencentAdapter(get=get)
    out = adapter.index_daily("A", "sh000001", START, END)
    assert out.columns == BAR_COLUMNS
    # 源形态原样入库：tencent_symbol 会把 sh000001 再折一次，问成别的票
    assert out["code"].unique().to_list() == ["sh000001"]
    assert get.calls[0][1].startswith("sh000001,day,")


def test_tencent_index_daily_hk_uses_kline_endpoint():
    payload = {"code": 0, "data": {"hkHSI": {"day": [["2024-01-02", "1", "2", "3", "4", "5"]]}}}
    get = FakeGet(payload)
    adapter = TencentAdapter(get=get)
    out = adapter.index_daily("HK", "hkHSI", START, END)
    assert out["close"].to_list() == [2.0]
    assert get.calls[0][0].endswith("kline/kline")


def test_tencent_index_daily_us_empty():
    """腾讯没有指数的美股链：这是一句答复（EmptyResponse），不是故障。"""
    adapter, _, _ = adapter_with(A_PAYLOAD)
    with pytest.raises(EmptyResponse):
        adapter.index_daily("US", "usINX", START, END)


def test_http_429_becomes_rate_limited(monkeypatch):
    import sams.datasource.tencent as module

    class Response429:
        status_code = 429

    monkeypatch.setattr("requests.get", lambda *a, **k: Response429())
    with pytest.raises(RateLimited):
        module.http_get("https://x", {}, timeout=1)


def test_degradation_succeeds_when_the_primary_has_no_us_data():
    """US 链的既景：Yahoo 被限流 → 腾讯接上。这是港美股从单源变两级的实际收益。"""
    from sams.datasource.registry import AdapterRegistry

    primary = TencentAdapter(get=FakeGet(RateLimited("tencent", "429")))
    backup = FakeGet(HK_PAYLOAD)
    registry = AdapterRegistry(
        adapters={"tencent": primary, "hkbackup": TencentAdapter(get=backup)},
        market_sources={"HK": ("tencent", "hkbackup")},
        attempts_per_source=1,
        sleep=lambda _s: None,
    )
    result = registry.fetch_daily("HK", "00700", START, END)
    assert result.source == "hkbackup" and result.degraded
    assert isinstance(result.frame, pl.DataFrame)


@pytest.mark.live
def test_live_tencent_covers_all_three_markets():
    """真接口：三个市场都要拿到根数不为 0 的最近日线。

    被标 live 的用例默认不跑（`-m 'not live'`）；改这个源时手动跑一次，
    它证明的是「我们对腾讯响应形态的理解还没过期」。
    """
    today = date.today()
    adapter = TencentAdapter()
    for market, code in (("A", "600519"), ("HK", "00700"), ("US", "AAPL")):
        frame = adapter.daily_bars(market, code, today, today)
        assert frame.height >= 1, f"{market} {code} 拿到空序列"
        assert frame["close"][0] > 0


@pytest.mark.live
def test_live_tencent_range_is_not_truncated():
    """请求一整年的日历天数，确认返回根数没被接口的上限截断。"""
    today = date.today()
    frame = TencentAdapter().daily_bars("A", "600519", today.replace(year=today.year - 1), today)
    assert frame.height > 200, f"只回来 {frame.height} 根，接口有隐性上限"


# --- 分时（D165）：`minute/query`，累计量折成股，美股在联网前就拒 ----------------------


class MinuteGet:
    """分时端点的参数是 `code=`，而 `FakeGet` 只认日线那串 `param=`：另记一份。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, url, params, *, timeout=15):
        self.calls.append((url, dict(params)))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def minute_payload(symbol="sh600519", day="20261028", rows=None, quote=None):
    rows = rows if rows is not None else [
        "0930 1354.98 1537 208260426.00",
        "0931 1353.00 3186 430459525.34",
        "0932 1352.10 4000 540000000.00",
    ]
    qt = {"market": "1", symbol: quote or ["1", "600519", "贵州茅台", "1332.08", "1355.90"]}
    qt[f"v_ff_{symbol}"] = ["x"]  # 实测：`qt` 里混着这个键与 `market`，按符号取才不端回整数
    return {"code": 0, "msg": "", "data": {symbol: {"data": {"date": day, "data": rows}, "qt": qt}}}


def test_minute_series_returns_the_declared_column_set_with_cumulative_volume_in_shares():
    """A 股分时：`date` 是 `20261028` 那一格，量按 手→股 换算（与日线同一口径）。"""
    frame = parse_tencent_minute(
        minute_payload(), market="A", code="600519", symbol="sh600519"
    )
    assert frame.columns == MINUTE_SERIES_COLUMNS
    assert frame["date"].to_list() == [DAY] * 3
    assert frame["time"].to_list() == ["09:30", "09:31", "09:32"]
    assert frame["price"].to_list() == [1354.98, 1353.00, 1352.10]
    # 1537 手 → 153700 股：差两个数量级的量柱在屏幕上读起来完全正常，所以只能钉数
    assert frame["volume"].to_list() == [153700.0, 318600.0, 400000.0]
    assert frame["amount"].to_list()[0] == pytest.approx(208260426.0)
    assert frame["prev_close"].to_list() == [1355.90] * 3
    assert frame["source"].to_list() == ["tencent"] * 3


def test_minute_series_reads_hk_volume_as_shares_without_scaling():
    """港股那格单位是股：乘 100 会把腾讯一天的量放大成它自己的十倍。"""
    frame = parse_tencent_minute(
        minute_payload(
            symbol="hk00700",
            rows=["0930 384.000 510600 196105924.933", "0931 384.200 900000 340000000.000"],
            quote=["1", "00700", "腾讯控股", "384.000", "385.200"],
        ),
        market="HK",
        code="00700",
        symbol="hk00700",
    )
    assert frame["volume"].to_list() == [510600.0, 900000.0]
    assert frame["prev_close"].to_list() == [385.20, 385.20]


def test_minute_series_indexes_qt_by_symbol_not_the_first_key():
    """`qt` 里还挂着 `market` 与 `v_ff_*`：`next(iter(qt))` 会端回一个整数或列表。

    这不是假想的脏数据，是实测的形状，所以它值得一条自己的用例。
    """
    payload = minute_payload()
    payload["data"]["sh600519"]["qt"] = {
        "market": "1",
        "v_ff_sh600519": ["a"],
        "sh600519": ["1", "600519", "贵州茅台", "1332.08", "1355.90"],
    }
    frame = parse_tencent_minute(payload, market="A", code="600519", symbol="sh600519")
    assert frame["prev_close"].to_list() == [1355.90] * 3


def test_minute_series_refuses_us_before_touching_the_network():
    """美股那一格实测只给三字段、`date` 是空串：它答不了「哪一天」，就直接不答这一档。

    把「没这市场」说成一次空答复会让降级链继续问下去、屏幕显示空白；
    在这里拒掉，健康页与周期条才知道该置灰。
    """
    get = MinuteGet({})
    adapter = TencentAdapter(get=get, clock=FrozenClock(DAY))
    with pytest.raises(EmptyResponse, match="只供"):
        adapter.minute_series("US", "AAPL", DAY, DAY)
    assert get.calls == []


def test_minute_series_refuses_a_session_outside_the_requested_window():
    """接口永远给「最近一个交易日」，请求的那天对不上就是没答：不许静默交回隔壁那天。"""
    adapter = TencentAdapter(
        get=MinuteGet(minute_payload(day="20261027")), clock=FrozenClock(DAY)
    )
    with pytest.raises(EmptyResponse, match="不在"):
        adapter.minute_series("A", "600519", DAY, DAY)


def test_minute_series_rejects_a_row_that_is_not_four_fields():
    """行少了字段是形态变了（`FieldDrift`），不是「今天没数据」：一次定性、立刻换源。"""
    with pytest.raises(FieldDrift, match="字段"):
        parse_tencent_minute(
            minute_payload(rows=["0930 1354.98 1537"]),
            market="A",
            code="600519",
            symbol="sh600519",
        )
