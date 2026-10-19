"""区间与赔率（M2-D，spec §8.3）：把分位与 PR 换成四个价位和一个赔率。

这一层的规矩只有两条：

1. **每一个数都能手算**。合理区间是 `现价 × 目标 PR ÷ 现在的 PR`，止损是 `现价 × (1-p)`
   或 `现价 - k×ATR`，赔率是 `(目标 - 现价) ÷ (现价 - 止损)` —— 屏幕上给出的式子就是这里
   算的那一条，读的人拿计算器能对上（黄金用例 `test_trade_plan_hand_computed_value_chain`）。
2. **算不出就说算不出**。缺哪一路的输入就拒哪一项，绝不退到「另一种止损」上凑一个数：
   一个 ATR 止损读起来像 8% 止损，是这一节唯一不可原谅的错。

胜率不在这里（`scoring.Composite` 是唯一出处，M5 才回测）：两处都会各自说一句「未回测」，
而它们可以不一致。
"""

from __future__ import annotations

from dataclasses import fields
from datetime import date

import pytest

from sams.analysis.valuation.compose import (
    Entry,
    Level,
    Plan,
    entry_zone,
    fair_band,
    pick_target,
    reward_risk,
    stop_level,
    trade_plan,
)
from sams.analysis.valuation.percentile import Percentile
from sams.analysis.valuation.styles import ENTRY_RULES, STOP_KINDS, TREND, VALUE
from sams.core.errors import ValidationError
from sams.core.fundamentals import BASIS_PB_ESTIMATE, BASIS_PE, BASIS_REFUSED


def pctl(value: float | None, reason: str = "") -> Percentile:
    return Percentile(
        value=value,
        current=1.0 if value is not None else None,
        samples=1200,
        metric="pr",
        window_from=date(2021, 10, 16),
        window_to=date(2026, 10, 16),
        reason=reason,
        evidence="valuation_daily.pe_ttm × financials.roe 逐日配对",
    )


def test_fair_band_scales_the_price_by_the_pr_it_should_reach():
    """`fair = 现价 × 目标 PR ÷ 现在的 PR`：PR=1 时就是现价的 0.8~1.2 倍。"""
    got = fair_band(price=30.0, pr=1.0, fair_pr_low=0.8, fair_pr_high=1.2, basis=BASIS_PE)
    assert got.ok
    assert got.low == pytest.approx(24.0)
    assert got.high == pytest.approx(36.0)
    assert "现价" in got.evidence and "30" in got.evidence
    assert got.key == "fair"


def test_fair_band_refuses_without_either_leg_of_the_ratio():
    for got in (
        fair_band(price=None, pr=1.0, fair_pr_low=0.8, fair_pr_high=1.2, basis=BASIS_PE),
        fair_band(price=30.0, pr=None, fair_pr_low=0.8, fair_pr_high=1.2, basis=BASIS_PE),
        fair_band(price=30.0, pr=0.0, fair_pr_low=0.8, fair_pr_high=1.2, basis=BASIS_PE),
        fair_band(price=30.0, pr=-2.0, fair_pr_low=0.8, fair_pr_high=1.2, basis=BASIS_PE),
    ):
        assert not got.ok
        assert got.reason, "拒绝出数却没说缺哪一路"


def test_fair_band_refuses_a_pr_that_refused_to_be_computed():
    """`basis=refused` 是 PR 自己拒了（D59），这里不能替它编一个合理区间。"""
    got = fair_band(price=30.0, pr=None, fair_pr_low=0.8, fair_pr_high=1.2, basis=BASIS_REFUSED)
    assert not got.ok
    assert "PR" in got.reason


def test_fair_band_says_which_pr_branch_the_band_inherits():
    """PB 那一支带着预估 ROE（D59）：合理区间也跟着继承那个预估。"""
    pe = fair_band(price=30.0, pr=1.0, fair_pr_low=0.8, fair_pr_high=1.2, basis=BASIS_PE)
    pb = fair_band(price=30.0, pr=1.0, fair_pr_low=0.8, fair_pr_high=1.2, basis=BASIS_PB_ESTIMATE)
    assert "预估" not in pe.evidence and "PE" in pe.evidence
    assert "预估" in pb.evidence and "PB" in pb.evidence


def test_fair_band_rejects_an_unknown_basis():
    with pytest.raises(ValidationError):
        fair_band(price=30.0, pr=1.0, fair_pr_low=0.8, fair_pr_high=1.2, basis="pe_ttm")


def test_entry_zone_near_fair_low_is_a_band_and_a_gate():
    got = entry_zone(
        rule="near_fair_low",
        price=30.0,
        fair_low=24.0,
        weekly_ma20=None,
        pr_percentile=10.0,
        entry_band_pct=0.05,
        percentile_entry_max=20.0,
    )
    assert isinstance(got, Entry)
    assert got.ok
    assert got.low == pytest.approx(22.8) and got.high == pytest.approx(25.2)
    assert got.ready is False, "现价 30 还在买入区上限（25.2）之上：条件没到"
    assert "现价" in got.reason and "25.2" in got.reason


def test_entry_zone_near_fair_low_is_ready_when_the_price_is_at_or_below_the_top():
    got = entry_zone(
        rule="near_fair_low",
        price=20.0,
        fair_low=24.0,
        weekly_ma20=None,
        pr_percentile=10.0,
        entry_band_pct=0.05,
        percentile_entry_max=20.0,
    )
    assert got.ready is True
    assert "已到" in got.evidence


def test_entry_zone_refuses_when_the_percentile_gate_is_not_met():
    """分位 45% 不是「便宜的那一档」，这一档压根不该给出买入区。"""
    got = entry_zone(
        rule="near_fair_low",
        price=30.0,
        fair_low=24.0,
        weekly_ma20=None,
        pr_percentile=45.0,
        entry_band_pct=0.05,
        percentile_entry_max=20.0,
    )
    assert not got.ok
    assert "20" in got.reason and "45" in got.reason


def test_entry_zone_refuses_without_the_percentile_reading():
    got = entry_zone(
        rule="near_fair_low",
        price=30.0,
        fair_low=24.0,
        weekly_ma20=None,
        pr_percentile=None,
        entry_band_pct=0.05,
        percentile_entry_max=20.0,
    )
    assert not got.ok
    assert "分位" in got.reason


def test_entry_zone_refuses_when_the_fair_band_it_hangs_on_is_missing():
    got = entry_zone(
        rule="near_fair_low",
        price=30.0,
        fair_low=None,
        weekly_ma20=None,
        pr_percentile=10.0,
        entry_band_pct=0.05,
        percentile_entry_max=20.0,
    )
    assert not got.ok
    assert "合理" in got.reason


def test_entry_zone_pullback_is_the_weekly_ma20_that_holds():
    """突破周 MA20 后回踩不破：买入区是「回到周均线上方的那一段」，不是追高。"""
    at_the_line = entry_zone(
        rule="weekly_ma20_pullback",
        price=26.5,
        fair_low=None,
        weekly_ma20=27.0,
        pr_percentile=None,
        entry_band_pct=0.03,
        percentile_entry_max=20.0,
    )
    assert at_the_line.ok
    assert at_the_line.low == pytest.approx(27.0 * 0.97)
    assert at_the_line.high == pytest.approx(27.0)
    assert at_the_line.ready is True

    above = entry_zone(
        rule="weekly_ma20_pullback",
        price=30.0,
        fair_low=None,
        weekly_ma20=27.0,
        pr_percentile=None,
        entry_band_pct=0.03,
        percentile_entry_max=20.0,
    )
    assert above.ready is False, "还悬在周均线上方：等回踩，不追"

    broken = entry_zone(
        rule="weekly_ma20_pullback",
        price=25.0,
        fair_low=None,
        weekly_ma20=27.0,
        pr_percentile=None,
        entry_band_pct=0.03,
        percentile_entry_max=20.0,
    )
    assert broken.ready is False
    assert "跌破" in broken.reason, "回踩「不破」才是这个规则，跌穿它不是便宜而是信号变了"


def test_entry_zone_pullback_refuses_without_the_weekly_line():
    got = entry_zone(
        rule="weekly_ma20_pullback",
        price=30.0,
        fair_low=24.0,
        weekly_ma20=None,
        pr_percentile=None,
        entry_band_pct=0.03,
        percentile_entry_max=20.0,
    )
    assert not got.ok
    assert "周MA20" in got.reason


def test_entry_zone_refuses_an_unknown_rule():
    with pytest.raises(ValidationError):
        entry_zone(
            rule="gap_up",
            price=30.0,
            fair_low=24.0,
            weekly_ma20=27.0,
            pr_percentile=10.0,
            entry_band_pct=0.05,
            percentile_entry_max=20.0,
        )


def stop_args(**over):
    given = {
        "kind": "pct",
        "price": 30.0,
        "stop_pct": 0.08,
        "atr_multiplier": 2.0,
        "fixed": 26.0,
        "atr": 1.5,
        "weekly_ma20": 27.0,
    }
    given.update(over)
    return stop_level(**given)


def test_every_declared_stop_kind_is_actually_implemented():
    """`styles.STOP_KINDS` 里写出来的每一种都得能算出一个价，且不许互相顶替。

    这一条是防「模板声明了 weekly_ma20，代码里掉回 pct」：屏幕上的止损价看起来都对，
    只有把它跟周均线放在一起时才知道它不是。
    """
    assert set(STOP_KINDS) == {"fixed", "pct", "atr2", "weekly_ma20"}
    tokens = {"fixed": "给定", "pct": "%", "atr2": "ATR", "weekly_ma20": "周MA20"}
    for kind in STOP_KINDS:
        got = stop_args(kind=kind)
        assert got.ok, f"{kind} 没有实现（或没能从自己的输入算出数）"
        assert got.low == got.high, "止损是一个点，不是一段"
        assert kind in got.evidence and tokens[kind] in got.evidence, (
            f"{kind} 的止损没写明它是从哪个数算出来的"
        )


def test_stop_values_are_the_formulas_on_the_screen():
    assert stop_args(kind="fixed").low == pytest.approx(26.0)
    assert stop_args(kind="pct").low == pytest.approx(27.6)
    assert stop_args(kind="atr2").low == pytest.approx(27.0), "30 - 2×1.5"
    assert stop_args(kind="weekly_ma20").low == pytest.approx(27.0)


@pytest.mark.parametrize(
    "kind",
    ["fixed", "pct", "atr2", "weekly_ma20"],
)
def test_stop_refuses_its_own_missing_input_instead_of_borrowing_another_kind(kind):
    missing = {
        "fixed": {"fixed": None},
        "pct": {"stop_pct": None},
        "atr2": {"atr": None},
        "weekly_ma20": {"weekly_ma20": None},
    }[kind]
    got = stop_args(kind=kind, **missing)
    assert not got.ok
    assert got.low is None, "拒了就别带一个别处来的价"
    assert got.reason


def test_stop_refuses_a_nonpositive_price_rather_than_rounding_to_zero():
    """现价 10、ATR 8、倍数 2 ⇒ -6：那不是「止损在 0」，是这几个数凑不出一个离场位。"""
    got = stop_args(kind="atr2", price=10.0, atr=8.0)
    assert not got.ok
    assert got.low is None, "悄悄夹到 0 会让赔率的分母变成整个现价"
    assert "-6" in got.reason


def test_stop_rejects_an_unknown_kind():
    with pytest.raises(ValidationError):
        stop_args(kind="trailing")


def test_target_takes_the_nearer_of_fair_high_and_previous_high():
    assert pick_target(price=30.0, fair_high=36.0, recent_high=34.0, lookback_days=250).low == (
        pytest.approx(34.0)
    )
    assert pick_target(price=30.0, fair_high=32.0, recent_high=34.0, lookback_days=250).low == (
        pytest.approx(32.0)
    )
    only_recent = pick_target(price=30.0, fair_high=None, recent_high=34.0, lookback_days=250)
    assert only_recent.low == pytest.approx(34.0)
    assert "合理上限" in only_recent.evidence, "少了一个候选也要说出来，别让它看起来像两个都算过"


def test_target_refuses_when_nothing_sits_above_the_price():
    """两个候选都在现价下方 ⇒ 上涨空间没有参照物，赔率在这一行上不该出现。"""
    got = pick_target(price=30.0, fair_high=28.0, recent_high=27.0, lookback_days=250)
    assert not got.ok
    assert "现价" in got.reason
    assert reward_risk(price=30.0, stop=27.6, target=None).ok is False


def test_reward_risk_is_the_formula_spec_wrote():
    got = reward_risk(price=30.0, stop=27.6, target=34.0)
    assert got.ok
    assert got.value == pytest.approx((34.0 - 30.0) / (30.0 - 27.6))
    assert "34" in got.evidence and "27.6" in got.evidence and "30" in got.evidence


def test_reward_risk_refuses_when_the_risk_leg_is_not_positive():
    """现价 ≤ 止损 时式子的分母 ≤ 0：那是「已经打到止损」，不是无穷大赔率。"""
    for got in (
        reward_risk(price=27.0, stop=27.6, target=34.0),
        reward_risk(price=27.6, stop=27.6, target=34.0),
        reward_risk(price=30.0, stop=None, target=34.0),
        reward_risk(price=None, stop=27.6, target=34.0),
    ):
        assert not got.ok
        assert got.reason


def trade_kwargs(**over):
    given = {
        "style": VALUE,
        "price": 30.0,
        "pr": 0.4,
        "basis": BASIS_PE,
        "pr_percentile": pctl(10.0),
        "recent_high": 36.0,
    }
    given.update(over)
    return given


def test_trade_plan_hand_computed_value_chain():
    """黄金样例：每一项都能拿计算器对上。

    现价 30、PR 0.4 ⇒ 合理区间 30×0.8÷0.4=60 到 30×1.2÷0.4=90；
    买入区 60±5% ⇒ 57~63，现价 30 在区间下方 ⇒ 条件已到；
    止损 8% ⇒ 30×0.92=27.6；目标取「合理上限 90」与「前高 36」里更近的 36；
    赔率 (36-30)÷(30-27.6)=6÷2.4=2.5。
    """
    plan = trade_plan(**trade_kwargs())
    assert isinstance(plan, Plan)
    assert plan.ok
    assert plan.fair.low == pytest.approx(60.0) and plan.fair.high == pytest.approx(90.0)
    assert plan.entry.low == pytest.approx(57.0) and plan.entry.high == pytest.approx(63.0)
    assert plan.entry.ready is True
    assert plan.stop.low == pytest.approx(27.6)
    assert plan.target.low == pytest.approx(36.0), "前高比 90 的近，spec 说取近的那个"
    assert plan.odds.value == pytest.approx(2.5)
    assert plan.style == "value"
    assert isinstance(plan.fair, Level)


def test_trade_plan_trend_style_reads_its_own_params_from_the_style():
    """模板换了止损的种类与买入的规则，`trade_plan` 不用被再告诉一遍。"""
    plan = trade_plan(
        style=TREND,
        price=30.0,
        pr=1.0,
        basis=BASIS_PE,
        weekly_ma20=27.0,
        recent_high=33.0,
    )
    assert plan.stop.low == pytest.approx(27.0), "TREND 的止损是周 MA20，不是 8%"
    assert "weekly_ma20" in plan.stop.evidence
    assert plan.entry.low == pytest.approx(27.0 * 0.97)
    assert plan.entry.ready is False
    assert plan.target.low == pytest.approx(33.0)
    assert plan.odds.value == pytest.approx((33.0 - 30.0) / (30.0 - 27.0))


def test_trade_plan_refusals_are_collected_not_swallowed():
    """缺多少路输入，屏幕上就该有多少句「这一项为什么没有」。"""
    plan = trade_plan(
        **trade_kwargs(
            pr=None,
            basis=BASIS_REFUSED,
            pr_percentile=pctl(None, reason="窗口内可比样本少于地板 250 天"),
        )
    )
    assert not plan.ok
    assert plan.entry.ready is None
    assert any("PR" in line for line in plan.refusals)
    assert any("样本" in line for line in plan.refusals), "分位自己的拒绝原因要原样传出来"


def test_trade_plan_uses_the_fair_high_as_target_when_there_is_no_previous_high():
    plan = trade_plan(**trade_kwargs(recent_high=None))
    assert plan.target.low == pytest.approx(90.0), "没有前高就用合理上限"
    assert plan.odds.value == pytest.approx((90.0 - 30.0) / (30.0 - 27.6))


def test_every_declared_entry_rule_is_implemented():
    assert set(ENTRY_RULES) == {"near_fair_low", "weekly_ma20_pullback"}
    for rule in ENTRY_RULES:
        got = entry_zone(
            rule=rule,
            price=26.5,
            fair_low=24.0,
            weekly_ma20=27.0,
            pr_percentile=10.0,
            entry_band_pct=0.05,
            percentile_entry_max=20.0,
        )
        assert got.ok, f"{rule} 没有实现"


def test_plan_does_not_carry_a_win_rate():
    """胜率只有一个出处（`scoring.Composite`，M5 回填）。两处就会各说一句「未回测」且可以不一致。"""
    names = {f.name for f in fields(Plan)}
    assert not any(name.startswith("win_rate") for name in names)
    assert {"style", "price", "fair", "entry", "stop", "target", "odds"} <= names


def test_plan_evidence_reads_as_one_traceable_page():
    """每个数都能追溯到它自己的那句来源 —— spec 那句「非黑盒」在屏幕上的形状。"""
    plan = trade_plan(**trade_kwargs())
    blob = plan.evidence
    for part in (plan.fair, plan.entry, plan.stop, plan.target):
        assert part.evidence in blob
    assert plan.odds.evidence in blob


def test_trade_plan_labels_the_lookback_the_caller_actually_used():
    """库里只有 120 根日线时，目标那行不能说「过去 250 日高点」。

    模板里那个数是**想要**的窗口；服务层带回来的是**用到**的根数，标签得跟着它换，
    否则屏幕上那句「前高」引用的是一个根本没查过的区间——而它与现价一样是减数。
    """
    narrow = trade_plan(
        style=VALUE,
        price=30.0,
        pr=0.4,
        basis=BASIS_PE,
        pr_percentile=pctl(10.0),
        recent_high=36.0,
        lookback_days=120,
    )
    assert "过去 120 日高点" in narrow.target.evidence
    wide = trade_plan(
        style=VALUE,
        price=30.0,
        pr=0.4,
        basis=BASIS_PE,
        pr_percentile=pctl(10.0),
        recent_high=36.0,
    )
    assert f"过去 {VALUE.param('target_lookback_days'):g} 日高点" in wide.target.evidence, (
        "不传就按模板那个数说，别在调用方留一个必须记得传的暗参数"
    )
