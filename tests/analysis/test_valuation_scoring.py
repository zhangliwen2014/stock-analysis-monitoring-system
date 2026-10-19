"""分项打分（M2-D，spec §8.3）：每一个 0~100 的分数都得带一句「它是从哪个字段算出来的」。

spec 给这一节的标题里写着**非黑盒**。这里实现的方式不是写一段说明文档，而是三条结构约束：

1. 每个分项是一个独立函数，输入是已经读出来的数（不是数据库、不是整张表）；
2. 每个 `Item` 出门就带着 `evidence`：来源字段名 + 计算窗口 + 实际用进去的那几个数；
3. 算不出就是算不出 —— `value=None` 加一句 `reason`，而不是补一个 50 分「中性」。
   补 50 分是让「没有数据」在合成里长得像「数据很平均」，那正是黑盒的入口。

合成那一侧（`composite`）只在**问到的**分项里加权平均，并且按拿到的权重比例决定出不出数：
权重覆盖不到地板（`min_covered_weight`）时拒绝合成，因为「三项里只有一项有数」的 78 分
与「九项都有数」的 78 分不是同一个东西。
"""

from __future__ import annotations

from datetime import date

import pytest

from sams.analysis.valuation.percentile import Percentile
from sams.analysis.valuation.scoring import (
    PR_SCORE_ANCHORS,
    Composite,
    Item,
    boll_position,
    composite,
    growth_score,
    ma_position,
    main_inflow_score,
    north_score,
    percentile_score,
    pr_score,
    roe_score,
    volume_follow,
    weekly_trend,
)
from sams.analysis.valuation.styles import ITEM_KEYS, TREND, VALUE
from sams.core.errors import ValidationError


def pctl(
    key: str,
    value: float | None,
    samples: int,
    reason: str = "",
    evidence: str = "valuation_daily 逐日配对",
) -> Percentile:
    """造一个分位读数：测试里只关心它带出来的样本数、拒绝原因与 evidence。"""
    return Percentile(
        value=value,
        current=1.0 if value is not None else None,
        samples=samples,
        metric=key,
        window_from=date(2021, 10, 17),
        window_to=date(2026, 10, 16),
        reason=reason,
        evidence=evidence,
    )


def test_every_item_names_its_source_fields_and_window():
    """「可解释」的机器定义：evidence 里有点名的字段名，也有实际进去的数。"""
    item = ma_position(close=12.0, ma20=11.0, ma60=10.0)
    assert item.key == "ma_position"
    assert "ma20" in item.evidence and "ma60" in item.evidence and "close" in item.evidence
    assert "12" in item.evidence and "11" in item.evidence

    roe = roe_score(0.18)
    assert "financials.roe" in roe.evidence
    assert "18" in roe.evidence, "百分数形态也要出现在 evidence 里：小数口径的 0.18 不好读"


def test_a_missing_input_refuses_rather_than_defaulting_to_neutral():
    """补一个 50 分的后果是「没有数据」在合成里看起来像「数据很平均」。"""
    for item in (
        ma_position(close=12.0, ma20=None, ma60=10.0),
        weekly_trend(weekly_close=None, weekly_ma20=10.0, weekly_ma20_prev=9.0),
        boll_position(close=12.0, upper=13.0, lower=None),
        volume_follow(price_change=0.02, volume=None, volume_ma=1000.0),
        roe_score(None),
        growth_score(revenue_yoy=None, profit_yoy=None),
        main_inflow_score(net_in=None, market_cap=1e10),
        north_score(north_net=None, market_cap=1e10),
    ):
        assert item.value is None, f"{item.key} 在缺输入时给出了分数"
        assert item.reason, f"{item.key} 拒绝出数却没说为什么"
        assert item.evidence, f"{item.key} 连来源都没说，屏幕上是空白"


def test_scores_land_in_zero_to_hundred():
    """越界的一个分数会让合成分越界，而两端的名字都还叫「分」。"""
    cases = [
        ma_position(close=99.0, ma20=11.0, ma60=10.0),
        ma_position(close=1.0, ma20=11.0, ma60=10.0),
        weekly_trend(weekly_close=12.0, weekly_ma20=10.0, weekly_ma20_prev=11.0),
        weekly_trend(weekly_close=8.0, weekly_ma20=10.0, weekly_ma20_prev=11.0),
        boll_position(close=13.0, upper=13.0, lower=11.0),
        volume_follow(price_change=-0.05, volume=2000.0, volume_ma=1000.0),
        roe_score(0.99),
        roe_score(-0.2),
        growth_score(revenue_yoy=0.9, profit_yoy=0.9),
        main_inflow_score(net_in=1e9, market_cap=1e10),
        main_inflow_score(net_in=-1e9, market_cap=1e10),
    ]
    for item in cases:
        assert item.value is not None
        assert 0.0 <= item.value <= 100.0, f"{item.key} 打出 {item.value}"


def test_ma_position_is_the_number_of_lines_price_stands_above():
    """这条要能被手算：站上 MA20 得 40，站上 MA60 得 40，短均在长均上方得 20。"""
    assert ma_position(close=12.0, ma20=11.0, ma60=10.0).value == pytest.approx(100.0)
    assert ma_position(close=12.0, ma20=13.0, ma60=10.0).value == pytest.approx(60.0), (
        "跌回短均下方但仍在长均上方：扣掉站上短均那 40，多头排列的 20 还在"
    )
    assert ma_position(close=9.0, ma20=11.0, ma60=13.0).value == pytest.approx(0.0)


def test_weekly_trend_is_above_plus_rising():
    assert weekly_trend(12.0, 10.0, 9.0).value == pytest.approx(100.0)
    assert weekly_trend(12.0, 10.0, 11.0).value == pytest.approx(50.0), "在线上方但均线在走平/下行"
    assert weekly_trend(8.0, 10.0, 11.0).value == pytest.approx(0.0)


def test_boll_position_reads_recovery_not_breakout():
    """%B=0（触下轨）给高分：这一项问的是「回调到位没有」。

    突破上轨是 M3 的规则模板（spec §10.3），不是打分项 —— 所以趋势型压根不问这一项
    （`styles.TREND` 的注释写了这件事）。名字与方向必须一起被钉住，否则读的人会以为
    「布林位置 5 分」是在说这只票弱。
    """
    assert boll_position(close=11.0, upper=13.0, lower=11.0).value == pytest.approx(100.0)
    assert boll_position(close=13.0, upper=13.0, lower=11.0).value == pytest.approx(0.0)
    assert boll_position(close=12.0, upper=12.0, lower=12.0).value is None, (
        "上下轨重合时带宽为 0：那是数据坏了，不是 %B=0.5"
    )


def test_volume_follow_is_a_four_quadrant_table():
    """涨+放量 / 涨+缩量 / 跌+放量 / 跌+缩量：四个格子，手算就能对上。"""
    assert volume_follow(0.03, 2000.0, 1000.0).value == pytest.approx(80.0)
    assert volume_follow(0.03, 500.0, 1000.0).value == pytest.approx(55.0)
    assert volume_follow(-0.03, 2000.0, 1000.0).value == pytest.approx(30.0)
    assert volume_follow(-0.03, 500.0, 1000.0).value == pytest.approx(45.0)
    assert volume_follow(0.0, 2000.0, 1000.0).value == pytest.approx(50.0), "平盘没有方向可判"


def test_volume_follow_evidence_prints_numbers_a_person_can_read():
    """evidence 是给人核对的那一行：把 `-0.0007192519779428697` 原样印出来，读的人得先数零
    才知道那是「跌 0.07%」——一行证据不该是一道换算题。"""
    item = volume_follow(-0.0007192519779428697, 34006900.0, 31765860.0)
    assert "-0.07%" in item.evidence
    assert "34,006,900" in item.evidence and "31,765,860" in item.evidence
    assert "34006900.0" not in item.evidence


def test_pr_score_anchors_carry_the_users_own_threshold():
    """`PR ≤ 0.4` 是需求原文里唯一的数，它在打分表上必须能对上一个具体的分数。"""
    assert pr_score(0.4).value == pytest.approx(90.0)
    assert pr_score(1.0).value == pytest.approx(60.0), "PR=1 是「一块钱利润付一块钱」= 公允刻度"
    assert pr_score(0.2).value == pytest.approx(100.0)
    assert pr_score(4.0).value == pytest.approx(0.0)
    assert pr_score(0.1).value == pytest.approx(100.0), "地板以下不额外奖励：已经足够便宜"
    assert pr_score(0.3).value == pytest.approx(95.0), "两个锚点之间线性插值，不做台阶"
    assert pr_score(9.0).value == pytest.approx(0.0)
    for low, high in zip(PR_SCORE_ANCHORS, PR_SCORE_ANCHORS[1:], strict=False):
        assert low[0] < high[0] and low[1] > high[1], "锚点必须单调递减，否则同一只票两个分数"


def test_pr_score_refuses_when_there_is_no_pr():
    refused = pr_score(None)
    assert refused.value is None and refused.reason


def test_percentile_score_is_inverted_and_carries_the_sample_count():
    """分位低 = 便宜 = 高分；而 20 分位（样本 1200 天）与 20 分位（样本 260 天）不是同一句话。"""
    low = percentile_score(pctl("pr", 10.0, 1200, evidence="逐日配对，样本 1200 天"))
    assert low.key == "pr_percentile"
    assert low.value == pytest.approx(90.0)
    assert "1200" in low.evidence and "10" in low.evidence

    thin = percentile_score(
        pctl("pe_ttm", None, 11, reason="窗口内只有 11 个可比交易日，少于地板 250 天")
    )
    assert thin.key == "pe_percentile"
    assert thin.value is None
    assert "250" in thin.reason, "拒绝的原因要能原样传出来，不是变成一句「无数据」"


def test_percentile_score_refuses_an_unmapped_metric():
    """分位的 `metric` 与这里的分项名对不上时只能报错：静默给个名字，屏幕上就是别的分项。"""
    with pytest.raises(ValidationError):
        percentile_score(pctl("roe", 50.0, 300))


def test_roe_and_growth_gate_the_caliber_before_scoring():
    """D66：一个忘了 ÷100 的 ROE 在这条链上的症状是 100 分，而不是一个偏高的数。"""
    assert roe_score(20.0).value is None
    assert "÷100" in roe_score(20.0).reason or "口径" in roe_score(20.0).reason
    assert roe_score(0.20).value == pytest.approx(85.0)

    both = growth_score(0.20, 0.40)
    assert both.value == pytest.approx(growth_score(0.30, 0.30).value), "取两者的平均"
    assert "revenue_yoy" in both.evidence and "profit_yoy" in both.evidence

    one = growth_score(0.20, None)
    assert one.value is not None
    assert "profit_yoy" in one.evidence or "profit_yoy" in one.reason, (
        "少了一项必须在 evidence 里说出来：合成的分数不能看起来像两项都算过"
    )


def test_money_flow_scores_are_scaled_by_market_cap_not_raw_amount():
    """主力净流入 1 亿在大盘股与小微盘上不是同一件事：比率才有刻度。"""
    big = main_inflow_score(1e8, 1e11)
    small = main_inflow_score(1e8, 1e9)
    assert big.value < small.value
    assert "market_cap" in big.evidence and "main_net_in" in big.evidence
    assert main_inflow_score(1e8, None).value is None, "没有市值就没法归一，不假设一个"
    assert north_score(None, 1e11).value is None, "北向那一列现在是 NULL（U25），不是 0"


def test_composite_weights_only_what_the_template_asked():
    """四项覆盖 57% 的权重（过地板 `min_covered_weight=50%`）⇒ 合成分只在这四项之间归一。"""
    items = [
        Item("pr", "市赚率", 90.0, evidence="pr"),
        Item("roe", "净资产收益率", 80.0, evidence="roe"),
        Item("growth", "成长性", 70.0, evidence="growth"),
        Item("pr_percentile", "市赚率历史分位", 60.0, evidence="pr_percentile"),
    ]
    got = composite(items, VALUE)
    assert got.ok
    weights = {item.key: VALUE.weight(item.key) for item in items}
    expected = sum(item.value * weights[item.key] for item in items) / sum(weights.values())
    assert got.score == pytest.approx(expected)
    assert got.covered_weight == pytest.approx(sum(weights.values()))
    assert sorted(got.missing) == sorted(set(VALUE.weights) - set(weights))


def test_composite_refuses_when_too_little_of_the_weight_has_data():
    """三项里有一项有数的 82 分，和九项都有数的 82 分，不是同一个东西。"""
    got = composite([Item("pr", "市赚率", 90.0, evidence="pr")], VALUE)
    assert not got.ok
    assert got.score is None
    assert got.covered_weight == pytest.approx(VALUE.weight("pr"))
    assert "权重" in got.reason and f"{VALUE.weight('pr'):.0%}" in got.reason, (
        "要说出覆盖了多大比例的权重：读的人才知道这是「勉强合成」还是「压根没法合成」"
    )
    assert len(got.missing) == len(VALUE.weights) - 1


def test_composite_never_asks_a_question_the_template_did_not():
    """传入一个该模板没问的分项 ⇒ 报错。静默收下它，等于让权重和悄悄变成 ≠1。"""
    with pytest.raises(ValidationError):
        composite([Item("boll_position", "布林位置", 50.0, evidence="x")], TREND)


def test_composite_reports_the_win_rate_as_unbacked_and_unsortable():
    """spec §8.3 的约束：`win_rate_samples < 30` 不得参与任何排序或推荐。

    M2 阶段压根没有胜率（M5 才回测），所以两个字段都是空 —— 而「空」与「0% 胜率」
    在排序里是同一种事故。
    """
    got = composite([Item("pr", "市赚率", 90.0, evidence="pr")], VALUE)
    assert got.win_rate is None
    assert got.win_rate_note == "未回测"
    assert got.sortable_by_win_rate is False

    backed = composite([Item("pr", "市赚率", 90.0, evidence="pr")], VALUE, win_rate=0.55)
    assert backed.win_rate == pytest.approx(0.55)
    assert backed.sortable_by_win_rate is False, "没有样本数的胜率仍然不许排序"

    enough = composite(
        [Item("pr", "市赚率", 90.0, evidence="pr")], VALUE, win_rate=0.55, win_rate_samples=40
    )
    assert enough.sortable_by_win_rate is True
    thin = composite(
        [Item("pr", "市赚率", 90.0, evidence="pr")], VALUE, win_rate=0.55, win_rate_samples=29
    )
    assert thin.sortable_by_win_rate is False
    assert "样本" in thin.win_rate_note


def test_items_cover_the_whole_vocabulary():
    """十二个分项各自都有一个 producer；少一个就是 `composite` 永远等不到它的数。"""
    producers = {
        "ma_position": ma_position(close=1.0, ma20=1.0, ma60=1.0),
        "weekly_trend": weekly_trend(1.0, 1.0, 1.0),
        "boll_position": boll_position(1.0, 2.0, 0.5),
        "volume_follow": volume_follow(0.01, 1.0, 1.0),
        "pr": pr_score(0.5),
        "roe": roe_score(0.1),
        "growth": growth_score(0.1, 0.1),
        "main_inflow": main_inflow_score(1.0, 1e9),
        "north_bound": north_score(1.0, 1e9),
    }
    percentile_items = [
        percentile_score(pctl(metric, 50.0, 300)) for metric in ("pr", "pe_ttm", "pb")
    ]
    produced = {item.key for item in list(producers.values()) + percentile_items}
    assert produced == set(ITEM_KEYS)
    assert isinstance(producers["pr"], Item)
    assert Composite is not None and Item is not None


def test_a_flow_refusal_can_carry_the_callers_sentence():
    """「这一段库里没有行」与「有行而这一列全空」的下一步命令不同，那句话由调用方带。

    写死一句「该源不给」会让人去换源，而真正该跑的是 `sams sync flow`。
    """
    mine = main_inflow_score(None, 1e11)
    assert "该源不给" in mine.reason, "不给 reason 时落回那句默认话"
    got = main_inflow_score(None, 1e11, reason="库里这段没有资金流行：先 `sams sync flow`")
    assert "sams sync flow" in got.reason and "该源不给" not in got.reason
    assert not north_score(None, 1e11, reason="没有行").ok
