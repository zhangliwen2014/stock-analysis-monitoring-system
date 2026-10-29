"""新浪外汇日线：汇率链的第二家（yfinance 被限流时仍能把 `fx_rates` 落满）。

这个源最危险的两件事：

* **方向**。代码是 `fx_s` + 两个币种小写，方向由拼接顺序决定，`fx_shkdcny` 与
  `fx_scnyhkd` 是两个不同的序列。把反方向收下，`fx_pnl` 每一笔都会差近 40%。
  所以解析要核对响应里的变量名，不认名字就不认数。
* **取哪一格**。一行五格里有开、低、高、收，取错一格仍然是一个「看起来正常」的汇率，
  只有对着下一行的开盘（= 前一日收盘）才看得出哪一格是收盘。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift, TransientError
from sams.datasource.sina import SinaAdapter, parse_sina_fx

D27 = date(2026, 10, 27)
D28 = date(2026, 10, 28)

#: 真实响应的形状：一段注释 + 一个 JSONP 变量，行与行之间用 `|` 隔开
HKD_JSONP = (
    "/*<script>location.href='//sina.com';</script>*/\n"
    'var _fx_shkdcny=("'
    "2026-10-27,0.8502461463,0.8498126163,0.85041245,0.8499209574,"
    "|2026-10-28,0.8499209574,0.8496609853,0.8503256747,0.8496682046"
    '");'
)


def test_the_close_is_the_fifth_field_not_the_last_quote():
    out = parse_sina_fx(HKD_JSONP, base="HKD", quote="CNY")
    assert out["date"].to_list() == [D27, D28]
    # 10-28 的开盘 = 10-27 的收盘：用这一对位置钉住「收盘是第 5 格」
    assert out["rate"].to_list() == [0.8499209574, 0.8496682046]
    assert out["base"].to_list() == ["HKD", "HKD"]
    assert out["quote"].to_list() == ["CNY", "CNY"]
    assert out["source"].to_list() == ["sina", "sina"]


def test_a_payload_for_the_other_direction_is_a_field_drift():
    """要 CNY→HKD，源给的是 HKD→CNY：反方向的序列不是这段汇率。"""
    with pytest.raises(FieldDrift, match="找不到"):
        parse_sina_fx(HKD_JSONP, base="CNY", quote="HKD")


def test_an_empty_body_is_an_answer_not_a_failure():
    with pytest.raises(EmptyResponse):
        parse_sina_fx('var _fx_shkdcny=("");', base="HKD", quote="CNY")


def test_a_non_text_payload_is_named():
    with pytest.raises(DatasourceError, match="JSONP"):
        parse_sina_fx([], base="HKD", quote="CNY")


class FakeFxGet:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []

    def __call__(self, url, params, *, timeout=15):
        self.calls.append(dict(params))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def test_the_adapter_asks_for_the_symbol_matching_the_direction():
    get = FakeFxGet(HKD_JSONP)
    adapter = SinaAdapter(fx_get=get)
    frame = adapter.fx_rate("HKD", "CNY", D27, D28)
    assert get.calls[0]["symbol"] == "fx_shkdcny"
    assert frame["rate"].to_list() == [0.8499209574, 0.8496682046]


def test_the_adapter_flips_the_symbol_with_the_requested_direction():
    payload = 'var _fx_scnyhkd=("2026-10-28,1.17658,1.17602,1.17694,1.17693");'
    get = FakeFxGet(payload)
    frame = SinaAdapter(fx_get=get).fx_rate("CNY", "HKD", D28, D28)
    assert get.calls[0]["symbol"] == "fx_scnyhkd"
    assert frame["rate"].to_list() == [1.17693]


def test_a_transport_error_is_classified_for_the_chain():
    with pytest.raises(TransientError):
        SinaAdapter(fx_get=FakeFxGet(ConnectionError("reset by peer"))).fx_rate(
            "HKD", "CNY", D27, D28
        )


@pytest.mark.live
def test_live_hkd_cny_rate_arrives():
    """可达性是「一刻的事实」，限流/掐连接时 skip；取错格才是真红。

    两周内的 HKD→CNY 在 0.5~1.5 之间：取成 100 倍（中行牌价那一套口径）
    或取到反方向（≈1.18）都会落在这个区间外或贴着上沿，一并钉住。
    """
    from sams.core.errors import RateLimited

    today = date.today()
    try:
        frame = SinaAdapter().fx_rate("HKD", "CNY", today - timedelta(days=14), today)
    except (RateLimited, TransientError) as exc:
        pytest.skip(f"新浪此刻拒绝本机：{exc}")
    assert frame.height >= 1
    assert frame["rate"].min() > 0
    assert 0.5 < frame["rate"].max() < 1.5
