"""分钟服务的三条职责各钉一枚：谁能答（D165）、累计差分、`adj_factor=1` 组装（D164）。"""

from __future__ import annotations

from datetime import date

from support import StubAdapter, StubMinuteAdapter, sample_bars

from sams.datasource.registry import AdapterRegistry
from sams.services.minute import MINUTE_SCALES, MinuteService

DAY = date(2026, 10, 28)


def registry(*adapters, markets=("A",)) -> AdapterRegistry:
    by_name = {a.name: a for a in adapters}
    return AdapterRegistry(
        adapters=by_name,
        market_sources={m: tuple(by_name) for m in markets},
        sleep=lambda seconds: None,
    )


def service(*adapters, markets=("A",)) -> MinuteService:
    return MinuteService(registry(*adapters, markets=markets), calendar=None)


def test_the_six_scales_are_the_only_minute_periods_the_screen_offers():
    assert MINUTE_SCALES == {"m1": 1, "m5": 5, "m15": 15, "m30": 30, "m60": 60, "m120": 120}


def test_availability_answers_per_kind_and_per_market():
    """两档各答各的：HK 答得了分时、答不了分钟 K，一条链不是一个 is/否。"""
    hk_and_a = StubMinuteAdapter("tencent", ["A", "HK"])
    got_a = service(hk_and_a, markets=("A", "HK"))
    assert (got_a.availability("A").series.ok, got_a.availability("A").bars.ok) == (True, True)
    hk = got_a.availability("HK")
    assert (hk.series.ok, hk.bars.ok) == (True, False)
    assert "分钟" in hk.bars.why_not


def test_availability_says_the_chain_has_no_source_at_all():
    """链上只有日线的源：那句话要说「没有能提供分钟K的源」，不是「这只票没数据」。"""
    answer = service(StubAdapter("akshare", ["A"], sample_bars())).availability("A").bars
    assert not answer.ok
    assert "没有能提供" in answer.why_not


def test_a_source_that_refuses_this_market_does_not_make_the_kind_answerable():
    """意愿表关掉这一档 → 不可答：置灰与接口 `note` 都从这一句出，不许两套说法。"""
    closed = StubMinuteAdapter("sina", ["A", "HK"], bars_markets=frozenset())
    assert not service(closed, markets=("A",)).availability("A").bars.ok
