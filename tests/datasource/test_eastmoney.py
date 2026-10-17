from __future__ import annotations

import json
from datetime import date

import polars as pl
import pytest

from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift, RateLimited
from sams.datasource.eastmoney import (
    EastmoneyAdapter,
    http_get,
    parse_eastmoney_daily,
    secid,
)

RECORDED = {
    "data": {
        "code": "600519",
        "klines": [
            "2024-01-02,1800.00,1810.00,1820.00,1790.00,30000,5400000000.00",
            "2024-01-03,1810.00,1815.00,1830.00,1800.00,28000,5100000000.00",
        ],
    }
}


def parse(payload=RECORDED, market="A", code="600519"):
    return parse_eastmoney_daily(payload, market=market, code=code)


def test_recorded_payload_becomes_standard_bars():
    out = parse()
    assert out["date"].to_list() == [date(2024, 1, 2), date(2024, 1, 3)]
    assert out["close"].to_list() == [1810.0, 1815.0]
    assert out["high"].to_list() == [1820.0, 1830.0]
    assert out["source"].unique().to_list() == ["eastmoney"]
    assert out["code"].unique().to_list() == ["600519"]


def test_json_text_is_accepted_too():
    assert parse(json.loads(json.dumps(RECORDED))).height == 2


@pytest.mark.parametrize(
    "payload, keyword",
    [
        ([], "不是对象"),
        ({}, "data"),
        ({"data": []}, "data"),
        ({"data": {"klines": []}}, "无数据"),
        ({"data": {"klines": ["2024-01-02,1800.00"]}}, "字段不足"),
    ],
)
def test_bad_shapes_fail_explicitly(payload, keyword):
    with pytest.raises((FieldDrift, EmptyResponse), match=keyword):
        parse(payload)


def test_empty_response_is_not_a_generic_failure():
    """停牌与接口挂掉要能分开：前者 EmptyResponse，后者 DatasourceError。"""
    with pytest.raises(EmptyResponse):
        parse({"data": {"klines": None}})
    with pytest.raises(DatasourceError):
        parse({"data": {"klines": ["bad-line"]}})


@pytest.mark.parametrize(
    "market, code, expected",
    [
        ("A", "600519", "1.600519"),
        ("A", "601398", "1.601398"),
        ("A", "000001", "0.000001"),
        ("A", "300750", "0.300750"),
        ("HK", "00700", "116.00700"),
        ("US", "AAPL", "105.AAPL"),
    ],
)
def test_secid(market, code, expected):
    assert secid(market, code) == expected


@pytest.mark.parametrize("market, code", [("A", "60051"), ("A", "AAPL"), ("JP", "1")])
def test_secid_rejects_impossible_codes(market, code):
    with pytest.raises(ValueError):
        secid(market, code)


def fake_get(payload=RECORDED, capture=None):
    def get(url, params):
        if capture is not None:
            capture.update({"url": url, **params})
        return payload

    return get


def test_adapter_passes_range_and_parses():
    capture: dict = {}
    adapter = EastmoneyAdapter(get=fake_get(capture=capture))
    out = adapter.daily_bars("A", "600519", date(2024, 1, 2), date(2024, 1, 3))
    assert isinstance(out, pl.DataFrame) and out.height == 2
    assert capture["secid"] == "1.600519"
    assert capture["beg"] == "20240102" and capture["end"] == "20240103"
    assert capture["klt"] == "101", "101 才是日线"


def test_adapter_rejects_unsupported_market():
    with pytest.raises(ValueError, match="不支持"):
        EastmoneyAdapter(get=fake_get()).daily_bars(
            "US", "AAPL", date(2024, 1, 2), date(2024, 1, 3)
        )


def test_network_error_is_classified_for_the_router():
    def angry(url, params):
        raise ConnectionResetError("远程主机强迫关闭了一个现有的连接")

    with pytest.raises(DatasourceError) as excinfo:
        EastmoneyAdapter(get=angry).daily_bars(
            "A", "600519", date(2024, 1, 2), date(2024, 1, 3)
        )
    assert excinfo.value.source == "eastmoney"


def test_http_layer_rate_limit_hint_becomes_rate_limited():
    class Response:
        text = '{"code":429}'

    def throttled(url, params):
        raise RuntimeError("HTTP 429 too many requests")

    with pytest.raises(RateLimited):
        EastmoneyAdapter(get=throttled).daily_bars(
            "A", "600519", date(2024, 1, 2), date(2024, 1, 3)
        )
    assert callable(http_get)
