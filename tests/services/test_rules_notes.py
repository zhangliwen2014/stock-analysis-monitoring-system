"""§10.2 的执行约束文案。约束绑**标的市场**，不绑账户（D21）。

这一文件里的每一句话都是为了让那条告警**不误导决策**：「止损已破」后面如果不跟一句
「而今天买的可能今天就卖不掉」，用户会按一个执行不了的计划去操作。
"""

from __future__ import annotations

from datetime import date

import pytest

from sams.services.rules.notes import Fx, constraint_lines, holding_pnl
from sams.services.rules.snapshot import Snapshot


def snap(**over) -> Snapshot:
    base = {
        "market": "A",
        "code": "601088",
        "name": "中国神华",
        "currency": "CNY",
        "report_currency": "CNY",
        "price": 9.8,
        "price_on": date(2026, 10, 20),
        "prev_close": 10.2,
        "avg_cost": 12.0,
        "qty": 100.0,
        "opened_on": date(2025, 3, 3),
    }
    base.update(over)
    return Snapshot(**base)


def test_a_position_opened_today_is_marked_t_plus_one() -> None:
    got = constraint_lines(snap(opened_on=date(2026, 10, 20)))
    # 那句话带上了「建仓日 == 现价日」这一天：现价陈旧时「今日」会不准，日期才查得出
    assert any("T+1" in line and "2026-10-20" in line for line in got)


def test_an_older_position_is_not_marked_t_plus_one() -> None:
    assert not any("T+1" in line for line in constraint_lines(snap()))


def test_a_us_symbol_is_t_plus_zero_and_says_so_only_when_it_matters() -> None:
    """美股没有 T+1 可说：写一条「可当日卖出」是废话，而不写才是对的。

    建仓日与现价日**同一天**（此前这里是 2025≠2026，T+1 分支的前提先就假了，
    市场并不是它沉默的理由）。把 `_SETTLEMENT` 里的 US 改成 T+1 会让这一枚变红。
    """
    got = constraint_lines(snap(market="US", code="AAPL", opened_on=date(2026, 10, 20)))
    assert not any("T+1" in line for line in got)


def test_a_hk_symbol_is_t_plus_zero_on_the_same_day() -> None:
    """同美股那一形态：建仓与现价同日，但 HK 是 T+0 ⇒ 没有 T+1 那句。"""
    got = constraint_lines(snap(market="HK", code="00700", opened_on=date(2026, 10, 20)))
    assert not any("T+1" in line for line in got)


def test_the_us_and_hk_silence_comes_from_the_market_not_missing_data() -> None:
    """反向钉：同一个建仓日，US/HK 只少了 T+1 那句，其它行照常 ⇒ 沉默是「市场」造成的，
    不是 `opened_on` 为空或其它前提先假了。换成 A 股同一天就会多出 T+1 那句。"""
    same_day = {"opened_on": date(2026, 10, 20), "margin_enabled": True}
    for market, code in (("US", "AAPL"), ("HK", "00700")):
        got = constraint_lines(snap(market=market, code=code, **same_day))
        assert any("维持担保" in line for line in got), market  # 这段确实产出了行
        assert not any("T+1" in line for line in got), market
    assert any("T+1" in line for line in constraint_lines(snap(**same_day)))


def test_a_limit_down_print_is_named_with_its_own_band() -> None:
    """主板 -10%、创业板/科创板 -20%：那句话里的数必须来自 `daily_price_limit`，不许写死。"""
    got = constraint_lines(snap(prev_close=10.9, price=9.81))
    assert any("跌停" in line and "10%" in line for line in got)


def test_a_wide_band_symbol_uses_the_wider_band() -> None:
    got = constraint_lines(snap(code="300750", prev_close=100.0, price=80.0))
    assert any("20%" in line for line in got)


def test_a_normal_down_day_is_not_called_a_limit_down() -> None:
    assert not any("跌停" in line for line in constraint_lines(snap()))


def test_one_cent_above_the_board_is_still_called_a_limit_down() -> None:
    """板 9.00，现价 9.01（高一分）仍算封板：那一分正是舍入的容许带。

    去掉 `_LIMIT_DOWN_TICK`（改成 0）会让这一枚变红。
    """
    got = constraint_lines(snap(prev_close=10.0, price=9.01))
    assert any("跌停" in line for line in got)


def test_a_day_down_but_not_at_the_board_is_not_called_a_limit_down() -> None:
    """跌停余量只有**一分**：跌 7%、跌 9% 都不是封板，别说那句假话（D96）。

    −9%（9.10）专门盯着「有人把余量又改成百分比」：离主板 9.00 还差一角，而一个按
    百分比留的余量（哪怕 1.5%）会把它误判成封板。
    """
    for price in (9.30, 9.10):  # prev_close=10.0 ⇒ −7% 与 −9%
        got = constraint_lines(snap(prev_close=10.0, price=price))
        assert not any("跌停" in line for line in got), f"price={price}"


@pytest.mark.parametrize(
    ("prev_close", "price"),
    [
        # 10.9 × 0.9 = 9.81：正好落在整分上，交易所与我们的 round 给同一个价
        (10.9, 9.81),
        # 4.35 × 0.9 = 3.915：交易所四舍五入到分给 3.92，而 round(3.915, 2) 因浮点给
        # 3.91 —— 真实板价比我们的 floor 高一分。那一分的余量就是为了认下它。
        (4.35, 3.92),
    ],
)
def test_the_limit_down_test_has_room_for_rounding(prev_close: float, price: float) -> None:
    """跌停判据留**一分**的余量：为的是交易所「四舍五入到分」，不是给百分比留后门。

    早先这里留 1.5%，自问自答写成「漏判比误判更伤所以放宽」。放宽的**方向**没变，
    变的是**量级**：只多容忍一分——多到把跌 8.5% 也说成封板的那句话是假的（D96）。
    """
    got = constraint_lines(snap(prev_close=prev_close, price=price))
    assert any("跌停" in line for line in got)


def test_a_margin_account_gets_the_maintenance_note() -> None:
    got = constraint_lines(snap(margin_enabled=True))
    assert any("维持担保比例" in line for line in got)
    assert any("M5" in line for line in got)  # 说清这一条只是提醒，规则本身没实现


def test_a_non_margin_account_does_not_claim_it() -> None:
    assert not any("维持担保" in line for line in constraint_lines(snap()))


def test_an_unrecognized_market_says_nothing_instead_of_crashing_the_run() -> None:
    """空 / 怪市场名：`canonical_market` 会抛，但这段文案引擎每趟都调——判不出就放行，
    不能一行脏数据把整趟告警打掉（取向同 `filters.no_open_alert`）。"""
    got = constraint_lines(snap(market=""))
    assert got == ()


def test_the_pnl_line_always_carries_the_symbol_currency() -> None:
    """§5.4 规则 3：金额一律带币种。-220.00 后面什么都不跟的话，美股那条会被读成人民币。"""
    got = holding_pnl(snap(currency="USD", report_currency="USD"))
    assert got.endswith("USD")
    assert got.startswith("浮亏 ")


def test_the_pnl_line_adds_the_report_currency_when_a_rate_is_given() -> None:
    got = holding_pnl(
        snap(currency="USD", report_currency="CNY"),
        fx=Fx(currency="CNY", rate=7.08, on=date(2026, 10, 17)),
    )
    # (9.8 − 12.0) × 100 = −220.00；1 单位 USD = 7.08 CNY ⇒ 折 220.00 × 7.08 = 1,557.60。
    # 钉的是**算出来的那个数**：把 `* fx.rate` 改成 `/ fx.rate` 会得 31.07 而让这一枚变红。
    assert "USD" in got and "折 CNY" in got and "7.08" in got and "2026-10-17" in got
    assert "1,557.60" in got


def test_a_rate_for_a_different_currency_than_the_report_is_not_used() -> None:
    """汇率只认汇总币种那一条：给了别种币的汇率，宁可说没读到也不印一个错的折合数。"""
    got = holding_pnl(
        snap(currency="USD", report_currency="CNY"),
        fx=Fx(currency="JPY", rate=110.0, on=date(2026, 10, 17)),
    )
    # 那句「没读到 {标的}→{汇总}」本身带「折合」二字，所以按 JPY 这个不该上屏的币来判：
    # 去掉 `fx.currency != report` 守卫会走到折合分支印出「折 JPY …」而让这一枚变红。
    assert "没读到" in got and "JPY" not in got


def test_a_cross_currency_pnl_without_a_rate_says_which_pair_is_missing() -> None:
    """不许默默省掉折合值：那一屏上只有标的币种，用户会以为那就是汇总口径。"""
    got = holding_pnl(snap(currency="USD", report_currency="CNY"))
    assert "USD→CNY" in got and "没读到" in got


def test_a_report_currency_that_matches_needs_no_conversion() -> None:
    got = holding_pnl(snap(), fx=None)
    assert got.endswith("CNY") and "折" not in got


def test_no_constraints_returns_an_empty_tuple() -> None:
    """三条约束都不成立（隔日建仓、没到板、非保证金）⇒ 空元组（docstring 承诺的形状）。"""
    assert constraint_lines(snap()) == ()


def test_a_missing_position_produces_no_pnl_line() -> None:
    """`holding_pnl` 的两条真分支：缺数量或缺成本都返回空串（`constraint_lines` 不读它们）。"""
    assert holding_pnl(snap(qty=None)) == ""
    assert holding_pnl(snap(avg_cost=None)) == ""


def test_a_missing_prev_close_is_not_called_a_limit_down() -> None:
    """没有前收盘就判不出涨跌幅：这时宁可不写那一行（取向与 T-12 一致）。"""
    assert not any("跌停" in line for line in constraint_lines(snap(prev_close=None)))


def test_constraint_text_is_a_pure_function_not_a_repo() -> None:
    """这段文案不许自己去查库：市场事实从 `Snapshot` 推、汇率由引擎递进来。

    取向同 `filters` 那条同名守卫（`test_the_gate_is_a_value_object_not_a_repo`）——
    比 import 方向更严：源码里连「storage」这个词都不该出现。
    """
    import inspect

    from sams.services.rules import notes

    assert "storage" not in inspect.getsource(notes)
