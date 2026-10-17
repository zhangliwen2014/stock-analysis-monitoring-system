"""腾讯 gtimg 适配器：录制响应 → 标准列集，以及三市场各自的参数形状。

这个源是港美股唯一的备源，所以它「静默给错东西」的几种形态都要有用例钉住：
`qfqday`（复权口径混进 raw）、`code!=0`（参数错却看起来像没数据）、
美股不带后缀（返回空 day 数组）。这三种在旧实现里都会变成「今天就是没数据」。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from sams.core.errors import EmptyResponse, FieldDrift, RateLimited, TransientError
from sams.datasource.tencent import (
    TencentAdapter,
    _bars_for,
    kline_param,
    parse_tencent_daily,
    parse_us_hint,
    tencent_symbol,
)

START, END = date(2024, 1, 2), date(2024, 1, 5)

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
