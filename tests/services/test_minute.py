"""分钟服务的三条职责各钉一枚：谁能答（D165）、累计差分、`adj_factor=1` 组装（D164）。"""

from __future__ import annotations

from datetime import date

import pytest
from support import StubAdapter, StubMinuteAdapter, sample_bars

from sams.core.errors import EmptyResponse
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


def test_minute_bars_are_assembled_with_factor_one_and_hfq_columns():
    """D164：分钟帧自己按 `adj_factor=1` 组装，不借 `adjusted_series`——它要的是日线除权。"""
    read = service(StubMinuteAdapter("tencent", ["A"]), markets=("A",)).bars(
        "A", "600519", period="m5", start=DAY, end=DAY, indicators=("ma",)
    )
    assert read.note is None
    frame = read.series.frame
    assert frame["adj_factor"].to_list() == [1.0] * 3
    assert {"open_hfq", "high_hfq", "low_hfq", "close_hfq"} <= set(frame.columns)
    # 因子是 1：`*_hfq` 与裸价逐字相等，这一条同时钉住「没有偷偷乘过别的」
    assert frame["close_hfq"].to_list() == frame["close"].to_list()
    assert frame["date"].to_list()[-1].strftime("%H:%M") == "11:05"


def test_minute_bars_carry_the_scale_to_the_chain():
    fake = StubMinuteAdapter("tencent", ["A"])
    service(fake, markets=("A",)).bars("A", "600519", period="m15", start=DAY, end=DAY)
    assert fake.extra == (15, 240)


def test_an_unknown_period_is_refused_as_a_bad_request():
    from sams.core.errors import ValidationError

    with pytest.raises(ValidationError, match="不是分钟档"):
        service(StubMinuteAdapter("tencent", ["A"])).bars(
            "A", "600519", period="m7", start=DAY, end=DAY
        )


def test_minute_bars_refusal_is_a_note_not_an_exception():
    """HK 的分钟 K 两个源都不答：接口要 200 + note（§6 钉 8），这里先把那句准备好。"""
    read = service(StubMinuteAdapter("tencent", ["A", "HK"], bars_markets=frozenset())).bars(
        "A", "600519", period="m5", start=DAY, end=DAY
    )
    assert read.series is None
    assert "分钟" in read.note


def test_a_dead_chain_gives_a_note_naming_the_source_that_failed():
    dead = StubMinuteAdapter("tencent", ["A"], outcome=EmptyResponse("tencent", "没有这一档"))
    read = service(dead, markets=("A",)).bars("A", "600519", period="m5", start=DAY, end=DAY)
    assert read.series is None
    assert "每个源都答了" in read.note


def test_the_frame_is_cached_so_a_second_indicator_pick_does_not_refetch():
    """缓存的键不含指标：换勾选读的是同一份数，而再打一次外网不是「换勾选」的语义。"""
    fake = StubMinuteAdapter("tencent", ["A"])
    reader = service(fake, markets=("A",))
    first = reader.bars("A", "600519", period="m5", start=DAY, end=DAY, indicators=("ma",))
    second = reader.bars("A", "600519", period="m5", start=DAY, end=DAY, indicators=("macd", "ma"))
    assert fake.calls == 1
    # 计划里写的是 `macd_26`：`INDICATORS["macd"]` 产出的是 `macd`/`macd_signal`/`macd_hist`，
    # 列名不带窗口数字，按仓库现实改成 `macd_hist`
    assert "macd_hist" in second.table.columns
    assert "macd_hist" not in first.table.columns
