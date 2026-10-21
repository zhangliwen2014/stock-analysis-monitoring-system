"""规则词汇表的行为契约（spec §10）。

这一整个模块没有 IO，所以用例就是在问「名单、默认值、拒绝的话」这三件事。
写在这里的东西会被仓库层、求值层、命令行三层各自读一遍——一处写错，三处口径分叉。
"""

from __future__ import annotations

import pytest

from sams.core.errors import ValidationError
from sams.core.rules import (
    DRIFT_TIER,
    FILTER_KEYS,
    HIT,
    PRE,
    RULE_KINDS,
    STAGE_RANK,
    STAGES,
    URGENT,
    Verdict,
    escalates,
    template_for,
    tier_of,
    validate_filters,
    validate_params,
)


def test_stage_order_is_pre_hit_urgent() -> None:
    """三档的次序只写一遍：冷却放行靠它（D94），文案里那一列也靠它。"""
    assert STAGES == ("pre", "hit", "urgent")
    assert STAGE_RANK[PRE] < STAGE_RANK[HIT] < STAGE_RANK[URGENT]


def test_every_kind_has_a_template_and_a_default_severity() -> None:
    for kind in RULE_KINDS:
        built = template_for(kind)
        assert built.kind == kind
        assert built.severity in ("strong", "mid", "weak")
        assert built.label, kind


def test_the_nine_stops_are_all_present() -> None:
    """§10.2 点名的九种，一种都不许漏：漏一种就是「止损体系完成」这句话是假的。"""
    expected = {
        "stop.fixed", "stop.pct", "stop.trailing", "stop.atr", "stop.ma",
        "stop.breakeven", "stop.time", "stop.thesis_drift", "tp.tiered",
    }
    assert expected <= set(RULE_KINDS)


def test_position_kinds_refuse_to_lose_their_account() -> None:
    """持仓类模板必须自己声明「我要挂在账户上」（§10.1 的去重键含 account_id）。"""
    assert template_for("stop.pct").needs_position is True
    assert template_for("price.cross_below").needs_position is False
    assert template_for("thesis.odds_below").needs_thesis is True


def test_an_unknown_kind_names_the_whole_list() -> None:
    with pytest.raises(ValidationError, match="stop.fixed"):
        template_for("stop.everything")


def test_a_missing_required_param_is_named_by_name() -> None:
    with pytest.raises(ValidationError, match="pct"):
        validate_params("stop.pct", {})


def test_defaults_are_filled_in_rather_than_left_absent() -> None:
    """求值层因此可以假定参数齐备：每个模板函数自己判默认值，就会有九种默认写法。"""
    assert validate_params("stop.atr", {}) == {"multiple": 2.0}


def test_a_number_out_of_range_is_refused_with_the_offending_value() -> None:
    with pytest.raises(ValidationError, match="1.5"):
        validate_params("stop.pct", {"pct": 1.5})


def test_a_stray_param_is_refused_instead_of_being_silently_ignored() -> None:
    """静默丢掉一个键，用户以为「止损比例 8%」生效了，而库里那条规则用的是默认值。"""
    with pytest.raises(ValidationError, match="percent"):
        validate_params("stop.pct", {"pct": 0.08, "percent": 8})


def test_a_choice_param_only_takes_the_listed_values() -> None:
    with pytest.raises(ValidationError, match="daily_ma60"):
        validate_params("stop.ma", {"basis": "hourly"})
    assert validate_params("stop.ma", {}) == {"basis": "weekly_ma20"}


def test_tiers_must_increase_and_end_at_the_target() -> None:
    assert validate_params("tp.tiered", {}) == {"tiers": [0.5, 1.0]}
    with pytest.raises(ValidationError, match="递增"):
        validate_params("tp.tiered", {"tiers": "1.0,0.5"})
    with pytest.raises(ValidationError, match="递增"):
        validate_params("tp.tiered", {"tiers": "0.5,0.5"})
    with pytest.raises(ValidationError, match="有限数"):
        validate_params("tp.tiered", {"tiers": "0.5,nan"})


def test_validation_accepts_its_own_output() -> None:
    """仓库层 update 会把读回来的 `params_json` 再校验一遍：自己的输出必须吃得下。"""
    once = validate_params("tp.tiered", {})
    assert validate_params("tp.tiered", once) == once


def test_a_non_numeric_param_says_what_it_expected() -> None:
    with pytest.raises(ValidationError, match="数字"):
        validate_params("stop.fixed", {"price": "十块"})
    with pytest.raises(ValidationError, match="有限数"):
        validate_params("stop.fixed", {"price": "nan"})  # nan 与任何线比都是 False


def test_filters_are_boolean_switches_from_a_closed_list() -> None:
    assert FILTER_KEYS == ("time_window", "data_freshness", "exclude_halt", "no_open_alert")
    # 返回的是补齐了四个默认值的整张表，不是一个只含被打开开关的稀疏字典
    assert validate_filters({"data_freshness": True}) == {
        **dict.fromkeys(FILTER_KEYS, False),
        "data_freshness": True,
    }
    assert validate_filters({}) == dict.fromkeys(FILTER_KEYS, False)


def test_market_rel_is_refused_because_it_has_no_input() -> None:
    """指数序列这一层根本没有：宁可不给这个开关，也不给一个恒真的过滤器。"""
    with pytest.raises(ValidationError, match="指数"):
        validate_filters({"market_rel": True})


def test_an_unknown_filter_names_the_four() -> None:
    with pytest.raises(ValidationError, match="no_open_alert"):
        validate_filters({"weekend": True})


def test_a_filter_value_must_be_a_bool() -> None:
    with pytest.raises(ValidationError, match="真或假"):
        validate_filters({"time_window": "yes"})


def test_a_verdict_carries_both_sides_of_the_comparison() -> None:
    built = Verdict(
        stage=HIT, value=9.8, threshold=10.0, value_text="9.80 元", threshold_text="10 元"
    )
    assert built.ok is True
    with pytest.raises(ValidationError, match="pre"):
        Verdict(stage="maybe", value=1.0, threshold=2.0, value_text="1", threshold_text="2")


def test_drift_tier_is_twenty_points() -> None:
    assert DRIFT_TIER == 20.0
    # 档位是 floor(总分 / 20)：100 分落在第 5 档（0~5 六档），不是封顶在第 4 档
    assert (tier_of(78.0), tier_of(61.0), tier_of(100.0)) == (3, 3, 5)
    assert tier_of(None) is None


def test_escalation_is_a_strict_rank_increase() -> None:
    """D94 的那道闸：同档与降档都不穿冷却，只有更响的一档穿。"""
    assert escalates(None, PRE) is True
    assert escalates(PRE, HIT) is True
    assert escalates(HIT, URGENT) is True
    assert escalates(HIT, HIT) is False
    assert escalates(URGENT, HIT) is False
    with pytest.raises(ValidationError, match="bogus"):
        escalates(PRE, "bogus")
