"""止损模板：纯函数，手搓 `Snapshot`。

这一文件里的每一枚都是 §10.2 那条强制验收的一部分——**除权日不许误报**。
所以断言里出现的都是「折回之后的数」，没有一处自己再乘一遍因子（那正是外套穿两件的样子）。
"""

from __future__ import annotations

from datetime import date

import pytest

from sams.core.errors import ValidationError
from sams.core.rules import HIT, PRE, URGENT, Verdict
from sams.services.rules.snapshot import Snapshot
from sams.services.rules.templates import (
    Ask,
    Outcome,
    money,
    stop_atr,
    stop_breakeven,
    stop_fixed,
    stop_ma,
    stop_pct,
    stop_stage,
    stop_trailing,
)


def snap(**over) -> Snapshot:
    base = {
        "market": "A",
        "code": "601088",
        "name": "中国神华",
        "currency": "CNY",
        "report_currency": "CNY",
        "price": 10.0,
        "price_on": date(2026, 10, 20),
        "prev_close": 10.2,
        "avg_cost": 12.0,
        "qty": 100.0,
    }
    base.update(over)
    return Snapshot(**base)


def ask(params: dict, **over) -> Ask:
    return Ask(snapshot=snap(**over), params=params)


def test_stage_order_looks_at_the_gap_first() -> None:
    """开盘在前收盘之上时不许报 `urgent`：那一档的意义正是「一开盘就没机会」。"""
    assert stop_stage(snap(day_open=10.8), 10.5) == HIT
    assert stop_stage(snap(day_open=9.8, prev_close=10.6), 10.5) == URGENT
    assert stop_stage(snap(price=10.7, day_open=10.7, prev_close=10.6), 10.62) == PRE
    assert stop_stage(snap(price=12.0), 10.0) is None
    assert stop_stage(snap(price=None), 10.0) is None


def test_money_always_carries_a_currency() -> None:
    """§5.4 规则 3：金额类阈值不允许不带币种。缺币种时明写「元」也不许留空。"""
    assert money(10.5, "USD") == "10.50 USD"
    assert money(None, "USD") == "未知"
    assert money(10.0, "") == "10.00 元"


def test_a_fixed_stop_names_the_line_and_the_currency() -> None:
    got = stop_fixed(ask({"price": 10.5}))
    assert got.hit is not None
    assert got.hit.stage == HIT
    assert got.hit.threshold_text == "10.50 CNY"
    assert got.hit.value_text == "10.00 CNY"


def test_a_fixed_stop_just_below_the_price_is_a_pre_warning() -> None:
    """线在现价下方 1%（不到 `PRE_BAND`）⇒ `pre`：这一档说的是「快到了」，还没破。"""
    got = stop_fixed(ask({"price": 9.9}))
    assert got.hit is not None
    assert got.hit.stage == PRE


def test_a_fixed_stop_well_below_the_price_says_nothing() -> None:
    got = stop_fixed(ask({"price": 9.0}))
    assert got == Outcome()  # 没触发是正常状态，不是「缺输入」，不该有一句话


def test_an_absent_price_is_named_instead_of_silently_false() -> None:
    """没有现价 ⇒ `why_not` 有话：屏幕上「没触发」与「这一趟没法判」必须是两句话。"""
    got = stop_fixed(ask({"price": 10.5}, price=None))
    assert got.hit is None
    assert "现价" in got.why_not


def test_a_gap_down_below_the_line_is_urgent_and_says_so() -> None:
    got = stop_fixed(ask({"price": 10.5}, day_open=9.8, prev_close=10.6))
    assert got.hit.stage == URGENT
    assert "开盘" in got.hit.why and "前收盘" in got.hit.why


def test_a_pct_stop_is_measured_from_cost_not_from_the_market() -> None:
    got = stop_pct(ask({"pct": 0.08}))  # 成本 12 → 线 11.04，现价 10 已破
    assert got.hit is not None
    assert got.hit.stage == HIT
    assert got.hit.threshold == pytest.approx(12.0 * (1 - 0.08))
    assert "成本" in got.hit.why


def test_a_pct_stop_without_a_position_explains_itself() -> None:
    got = stop_pct(ask({"pct": 0.08}, avg_cost=None, qty=None))
    assert got.hit is None
    assert "成本" in got.why_not


def test_a_pct_stop_with_a_non_positive_cost_explains_itself() -> None:
    """0 成本不是「最便宜的成本」：它会让线落在 0，那条止损从此永远不响。"""
    got = stop_pct(ask({"pct": 0.08}, avg_cost=0.0))
    assert got.hit is None
    assert "成本" in got.why_not


def test_trailing_uses_the_holding_period_high() -> None:
    got = stop_trailing(ask({"pct": 0.10}, holding_high=15.0))
    assert got.hit.threshold == pytest.approx(13.5)
    assert got.hit.stage == HIT  # 现价 10 < 13.5


def test_trailing_refuses_to_invent_a_high_when_the_position_is_new() -> None:
    """`holding_high=None`（不足三根 K，T-6 给的 None）时不许拿现价凑。

    拿现价凑的话回撤恒为 0，那条线永远贴在现价上方 10%，第一次呼吸就报止损。
    """
    got = stop_trailing(ask({"pct": 0.10}, holding_high=None))
    assert got.hit is None
    assert "最高价" in got.why_not


def test_a_verdict_is_rejected_when_the_stage_is_not_one_of_three() -> None:
    with pytest.raises(ValidationError, match="stage"):
        Verdict(stage="breaking", value=1.0, threshold=2.0, value_text="1", threshold_text="2")


def test_atr_stop_is_cost_minus_multiple_of_the_folded_atr() -> None:
    """ATR 是后复权口径算出来的，必须已经被 T-6 折回。这里再乘一次就是两件外套。"""
    got = stop_atr(ask({"multiple": 2.0}, atr=0.5))  # 12 − 1.0 = 11，现价 10 ⇒ hit
    assert got.hit.threshold == pytest.approx(11.0)
    assert got.hit.stage == HIT
    assert "ATR" in got.hit.why


def test_a_missing_atr_is_named_instead_of_defaulting_to_zero() -> None:
    """缺 ATR 时不许按 0 算：那条线会正好贴在成本上，现价低于成本一分都报止损。"""
    got = stop_atr(ask({"multiple": 2.0}, atr=None))
    assert got.hit is None
    assert "ATR" in got.why_not


def test_an_atr_multiple_that_pushes_the_line_below_zero_is_refused() -> None:
    """线算到 0 以下是一条永不触发的规则，而屏幕上它显示着「启用」。"""
    got = stop_atr(ask({"multiple": 10.0}, atr=1.5))  # 12 − 15 < 0
    assert got.hit is None
    assert "永远不会触发" in got.why_not


def test_ma_stop_takes_the_weekly_line_by_default() -> None:
    got = stop_ma(ask({"basis": "weekly_ma20"}, weekly_ma20=11.0))
    assert got.hit.threshold == pytest.approx(11.0)
    assert "周 MA20" in got.hit.why


def test_ma_stop_can_take_the_daily_sixty() -> None:
    got = stop_ma(ask({"basis": "daily_ma60"}, daily_ma60=9.0))
    assert got.hit is None  # 现价 10 > 9，也还没进 9×1.03 之内 ⇒ 两档都不报
    assert got == Outcome()  # quiet 路径只有真正读到日线且三档全不报才会返回


def test_a_missing_ma_names_which_line_is_missing() -> None:
    got = stop_ma(ask({"basis": "daily_ma60"}, daily_ma60=None))
    assert got.hit is None
    assert "日 MA60" in got.why_not


def test_breakeven_raises_the_line_after_the_gain_was_once_reached() -> None:
    """浮盈「曾经」到过 x% 才把线抬到成本——判据是持仓期最高价，不是今天的现价。

    用今天的现价判的话，一条涨过 20% 又跌回去的仓反而「没触发」，而它正是这一条要救的那种。
    """
    got = stop_breakeven(ask({"trigger_pct": 0.15}, holding_high=15.0))
    assert got.hit.threshold == pytest.approx(12.0)  # 抬到成本
    assert got.hit.stage == HIT
    assert "成本" in got.hit.why


def test_breakeven_stays_quiet_when_the_trigger_was_never_hit() -> None:
    got = stop_breakeven(ask({"trigger_pct": 0.15}, holding_high=12.5))  # 只涨过 4.2%
    assert got.hit is None
    assert got.why_not == ""


def test_a_breakeven_trigger_reached_exactly_is_not_lost_to_float_rounding() -> None:
    """成本 10、最高 11.5 恰好 +15%：浮点算出来是 0.1499999999999999，不能因此不抬线。"""
    got = stop_breakeven(
        ask({"trigger_pct": 0.15}, avg_cost=10.0, holding_high=11.5, price=9.9)
    )
    assert got.hit is not None
    assert got.hit.threshold == pytest.approx(10.0)
    assert got.hit.stage == HIT


def test_breakeven_without_a_position_has_no_cost_to_breakeven_against() -> None:
    got = stop_breakeven(
        ask({"trigger_pct": 0.15}, avg_cost=None, qty=None, holding_high=None)
    )
    assert got.hit is None
    assert "成本" in got.why_not


def test_atr_stop_without_a_position_has_no_line_to_measure_from() -> None:
    got = stop_atr(ask({"multiple": 2.0}, avg_cost=None, qty=None, atr=0.5))
    assert got.hit is None
    assert "成本" in got.why_not


def test_breakeven_without_a_holding_high_refuses_to_guess() -> None:
    """有成本、没有持仓期最高价：判不出「曾经到过 x%」，必须有话而不是静默。"""
    got = stop_breakeven(ask({"trigger_pct": 0.15}, holding_high=None))
    assert got.hit is None
    assert "最高价" in got.why_not


def test_breakeven_is_quiet_when_the_high_never_exceeded_cost() -> None:
    """持仓期最高价还低于成本：那 15% 从没到过，静默是正确状态，不是缺输入。"""
    got = stop_breakeven(ask({"trigger_pct": 0.15}, holding_high=11.0))
    assert got == Outcome()
