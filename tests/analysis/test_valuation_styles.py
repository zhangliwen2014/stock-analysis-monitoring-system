"""打分模板（M2-D，spec §8.3）：`value` / `trend` 两套内置权重，加上「可复制改参数」这条路。

这里没有计算，只有一份**词表**：分项的名字、每个分项属于哪一面（技术/基本/资金）、
每个模板给谁多少权重、以及合成那一侧唯一能接受的参数取值。词表上打错一个字的症状不是报错，
而是「那个分项永远拿不到数据，屏幕上却照旧给一个合成分」。
"""

from __future__ import annotations

import pytest

from sams.analysis.valuation.styles import (
    ENTRY_RULES,
    FUNDAMENTAL_ITEMS,
    FUNDING_ITEMS,
    ITEM_KEYS,
    PARAM_KEYS,
    STOP_KINDS,
    TECHNICAL_ITEMS,
    TREND,
    VALUE,
    Style,
    get_style,
    style_names,
)
from sams.core.errors import ValidationError


def test_the_three_faces_are_named_and_do_not_overlap():
    """spec §8.3 那三面就是这三组，而一个分项只能属于一面。

    同名进两组时，打印会把它数两遍、而权重表里只有一份 —— 屏幕上「技术面 40 分」与
    「基本面 40 分」于是指的是同一个分项。
    """
    groups = [set(TECHNICAL_ITEMS), set(FUNDAMENTAL_ITEMS), set(FUNDING_ITEMS)]
    for i, left in enumerate(groups):
        for right in groups[i + 1 :]:
            assert not (left & right), f"{left & right} 同时被归进两个面"
    assert set(ITEM_KEYS) == groups[0] | groups[1] | groups[2]
    assert all(groups), "某一面空了：模板还在给一个不存在的面打分"


def test_every_weight_key_is_a_named_item():
    """权重表里出现一个不在 `ITEM_KEYS` 里的名字，它不会报错 —— 它只是永远拿不到数据。"""
    for style in (VALUE, TREND):
        stray = set(style.weights) - set(ITEM_KEYS)
        assert stray == set(), f"{style.name} 有权重落在没有定义的分项上: {sorted(stray)}"
        assert len(set(ITEM_KEYS) - set(style.weights)) < len(ITEM_KEYS), (
            f"{style.name} 一个分项都没问：没被问到是**省略**，"
            "与「问了而没数据」是两件事，一条都不问的模板不配叫打分"
        )


def test_weights_sum_to_one_and_none_are_zero():
    """和为 1 是「合成分仍是 0~100」的全部前提；不想问一个分项就该删掉它，而不是写 0。"""
    for style in (VALUE, TREND):
        total = sum(style.weights.values())
        assert abs(total - 1.0) < 1e-9, f"{style.name} 的权重和是 {total}"
        assert all(w > 0 for w in style.weights.values()), f"{style.name} 里有 0 权重项"


def test_value_leans_on_the_fundamentals_and_trend_on_the_technicals():
    """两套模板的区别必须是可数的：不然内置两套只是两个名字。"""
    fundamental = set(FUNDAMENTAL_ITEMS)
    technical = set(TECHNICAL_ITEMS)
    assert sum(w for k, w in VALUE.weights.items() if k in fundamental) > sum(
        w for k, w in TREND.weights.items() if k in fundamental
    )
    assert sum(w for k, w in TREND.weights.items() if k in technical) > sum(
        w for k, w in VALUE.weights.items() if k in technical
    )
    assert VALUE.weight("pr") > TREND.weight("pr"), (
        "用户要的那条「按 PR 选股」是价值型的招牌，趋势型不该给它同一个权重"
    )


def test_the_entry_percentile_gate_is_a_param_not_a_buried_number():
    """spec §8.3 那句「价值型：分位 <20% 且接近 fair_low」里的门槛必须能被改。"""
    assert 0 < VALUE.param("percentile_entry_max") < 100
    assert VALUE.param("percentile_entry_max") == 20.0


def test_get_style_names_the_available_ones_when_the_answer_is_no():
    assert get_style("value") is VALUE
    assert get_style("trend") is TREND
    assert set(style_names()) == {"value", "trend"}
    with pytest.raises(ValidationError) as exc:
        get_style("momentum")
    assert "momentum" in str(exc.value)
    assert "value" in str(exc.value) and "trend" in str(exc.value), (
        "报错要能自己说完「那能填什么」：命令行不许 import 分析层，这份名单只有这里能给"
    )


def test_a_copied_style_can_change_weights_and_params():
    """「用户可复制改参数」= 有一条产出新 Style 的路，而不是一句文档承诺。"""
    mine = VALUE.with_overrides(weights={"pr": 0.5}, params={"stop_kind": "atr2"})
    assert mine.weight("pr") > VALUE.weight("pr"), "改完确实变重了"
    assert mine.param("stop_kind") == "atr2"
    assert VALUE.param("stop_kind") != "atr2", "复制改参数不许回头改到内置模板"
    assert mine.name == "value.copy"


def test_weights_are_relative_not_absolute():
    """这一条把语义钉死：给的数是**相对轻重**，落地前除以总和。

    写成「绝对权重」的话，用户改了 `pr` 一项就会让整张表加起来不是 1，而合成分悄悄
    越出 0~100 那一格。归一是唯一能让「改一项」仍然自洽的解释。
    """
    mine = VALUE.with_overrides(weights={"pr": 0.5})
    # 原来的 pr 是 0.18，换成 0.5 之后总和 1.32
    assert mine.weight("pr") == pytest.approx(0.5 / 1.32)
    assert abs(sum(mine.weights.values()) - 1.0) < 1e-9


def test_a_copy_renormalizes_rather_than_assuming_the_new_numbers_add_up():
    """配置文件里写的是「pr 权重 5」这种人的数，不是概率：合并后必须重新归一。"""
    mine = VALUE.with_overrides(weights={"pr": 5.0})
    assert abs(sum(mine.weights.values()) - 1.0) < 1e-9
    assert mine.weight("pr") < 1.0
    assert mine.weight("roe") < VALUE.weight("roe"), "别的项按比例被压小，不是被清掉"


def test_a_copy_refuses_a_stray_item_a_negative_weight_and_an_unknown_param():
    with pytest.raises(ValidationError):
        VALUE.with_overrides(weights={"nonsense": 0.2})
    with pytest.raises(ValidationError):
        VALUE.with_overrides(weights={"pr": -0.1})
    with pytest.raises(ValidationError):
        VALUE.with_overrides(params={"stop_kind": "trailing"})
    with pytest.raises(ValidationError):
        VALUE.with_overrides(params={"unknown_param": 1})


def test_every_style_carries_every_declared_param():
    """参数表是合成那侧的输入面：少一个键它会读到 None 而不是报错，多一个键没人读。"""
    for style in (VALUE, TREND):
        stray = set(style.params) - set(PARAM_KEYS)
        assert stray == set(), f"{style.name} 写了不在参数表上的 {sorted(stray)}"
        missing = set(PARAM_KEYS) - set(style.params)
        assert missing == set(), f"{style.name} 缺参数 {sorted(missing)}"


def test_the_stop_and_entry_vocabularies_are_exactly_what_compose_implements():
    """§10.2 那九种止损是**规则引擎**的目录；合成这一侧只做 spec §8.3 点名的四种。

    把九种都写进这张表，`--stop` 就会接受一个 compose 算不出来的取值。
    """
    assert set(STOP_KINDS) == {"fixed", "pct", "atr2", "weekly_ma20"}
    assert set(ENTRY_RULES) == {"near_fair_low", "weekly_ma20_pullback"}
    assert isinstance(STOP_KINDS, tuple) and isinstance(ENTRY_RULES, tuple)
    assert isinstance(VALUE, Style) and isinstance(TREND, Style)
