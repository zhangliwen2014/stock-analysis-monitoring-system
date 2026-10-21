"""四个过滤开关（§10.1「降噪主力」）。全部是纯函数：上下文由引擎备好传进来。

这里贯穿一条取向：**判不出来就放行**。这些开关存在的理由是少推噪音，不是拦截信号；
「日历读不到 ⇒ 一律压制」会让所有规则在那一天全体沉默，而那正是 §1 表里最贵的一行——
屏幕上「一切正常」，实际是一条真止损没人听见。所以每一枚测试都同时钉两侧：
该压的压住，该放的（上下文缺失）一定放。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from sams.core.rules import HIT, PRE, URGENT, Verdict, template_for
from sams.core.timeutil import Session
from sams.services.rules.filters import Gate, OpenAlert, run_filters
from sams.services.rules.snapshot import Snapshot

MORNING_OPEN = datetime(2026, 10, 20, 9, 30, tzinfo=UTC)
SESSIONS = (Session("morning", MORNING_OPEN, MORNING_OPEN + timedelta(hours=2)),)


def at(minutes: int) -> Gate:
    """开盘前后 `minutes` 分钟那个时刻的 Gate：负数是开盘**前**（集合竞价）。"""
    return Gate(now=MORNING_OPEN + timedelta(minutes=minutes), sessions=SESSIONS)


def snap(**over) -> Snapshot:
    base = {
        "market": "A",
        "code": "601088",
        "name": "中国神华",
        "currency": "CNY",
        "price": 9.8,
        "price_on": date(2026, 10, 20),
        "price_age_days": 0,
        "prev_close": 10.2,
    }
    base.update(over)
    return Snapshot(**base)


def hit(stage: str = HIT) -> Verdict:
    return Verdict(
        stage=stage,
        value=9.8,
        threshold=10.0,
        value_text="9.80 CNY",
        threshold_text="10.00 CNY",
    )


def run(
    filters: dict,
    gate: Gate,
    snapshot: Snapshot | None = None,
    verdict: Verdict | None = None,
    kind: str = "stop.fixed",
) -> str:
    return run_filters(
        filters,
        snapshot=snapshot or snap(),
        gate=gate,
        verdict=verdict or hit(),
        built=template_for(kind),
    )


def test_all_four_switches_off_suppresses_nothing() -> None:
    """默认全关（`validate_filters({})` 给的就是全关）：过滤器是 opt-in 的降噪，不是默认行为。"""
    assert run({}, Gate()) == ""


def test_time_window_suppresses_inside_the_minutes_after_the_open() -> None:
    got = run({"time_window": True}, at(3))
    assert "开盘" in got and "5" in got


def test_time_window_covers_the_call_auction_minutes_too() -> None:
    """§10.1 的原话是「开盘前 5 分钟」：A 股 9:15–9:25 的竞价价不是成交出来的价。"""
    assert run({"time_window": True}, at(-3)) != ""


def test_time_window_passes_once_the_window_is_over() -> None:
    assert run({"time_window": True}, at(6)) == ""


def test_the_window_is_half_open_on_both_flanks() -> None:
    """整 5 分钟这个界上两侧各钉一枚：两侧的答案不同，所以 `<=`/`<` 写反一定会被抓住。

    开盘**前**那一侧是闭的（9:25 整还在竞价里），开盘**后**那一侧是开的（9:35 整已经在
    连续竞价里了）。写成 `open - w <= now <= open + w` 会让 9:35 那条真破位沉默 1 分钟。
    """
    assert run({"time_window": True}, at(-5)) != ""
    assert run({"time_window": True}, at(5)) == ""


def test_time_window_leaves_a_non_price_kind_alone() -> None:
    """披露日提醒不是价格类：开盘那 5 分钟压不掉一条「该复核这条论」。"""
    assert run({"time_window": True}, at(1), kind="scheduled.reevaluate") == ""


def test_time_window_passes_when_the_sessions_are_not_read() -> None:
    assert run({"time_window": True}, Gate(now=MORNING_OPEN)) == ""


def test_data_freshness_suppresses_a_price_older_than_the_bound() -> None:
    got = run({"data_freshness": True}, Gate(), snap(price_age_days=4))
    assert "4 天前" in got and "2026-10-20" in got


def test_data_freshness_passes_at_the_bound_it_defends() -> None:
    """3 天正好是那个界：`>` 而不是 `>=`，否则周末一次的读数天天被压。"""
    assert run({"data_freshness": True}, Gate(), snap(price_age_days=3)) == ""


def test_data_freshness_suppresses_a_missing_price() -> None:
    assert "现价" in run({"data_freshness": True}, Gate(), snap(price=None, price_age_days=None))


def test_data_freshness_leaves_a_thesis_kind_alone() -> None:
    """赔率那条规则读的是价位的比，不是「今天的价格」——行情旧三天不影响它说话。"""
    stale = snap(price_age_days=30)
    assert run({"data_freshness": True}, Gate(), stale, kind="thesis.odds_below") == ""


def test_exclude_halt_suppresses_when_the_last_bar_is_before_the_last_session() -> None:
    """判据是「该市场最近一个交易日没有这根行」，不是停牌的权威数据。

    §10.3 把停复牌列为 M5 的事件类：现在没有那个源。这里用的是同方向的证据，
    而它命中新股未上市与节假日未同步时**也应该**压制——三种解释都指向「今天没有价」。
    """
    gate = Gate(last_session_on=date(2026, 10, 20))
    got = run({"exclude_halt": True}, gate, snap(price_on=date(2026, 10, 16)))
    assert "2026-10-20" in got and "2026-10-16" in got


def test_exclude_halt_passes_when_the_bar_is_current() -> None:
    assert run({"exclude_halt": True}, Gate(last_session_on=date(2026, 10, 20))) == ""


def test_no_open_alert_suppresses_a_repeat_of_the_same_stage() -> None:
    gate = Gate(open_alert=OpenAlert(id="al-1", stage=HIT, on=date(2026, 10, 20)))
    got = run({"no_open_alert": True}, gate)
    assert "al-1" in got and "未处理" in got


def test_no_open_alert_suppresses_a_quieter_repeat() -> None:
    gate = Gate(open_alert=OpenAlert(id="al-1", stage=URGENT, on=date(2026, 10, 20)))
    assert run({"no_open_alert": True}, gate, verdict=hit(HIT)) != ""


def test_no_open_alert_lets_an_escalation_through() -> None:
    """与 D94 同一个道理：未处理的那条是 `pre`，现在真跌破了 ⇒ 压不掉。

    「已有一条没点掉的预警」恰恰是不能用来堵住止损的理由。
    """
    gate = Gate(open_alert=OpenAlert(id="al-1", stage=PRE, on=date(2026, 10, 20)))
    assert run({"no_open_alert": True}, gate, verdict=hit(HIT)) == ""


def test_no_open_alert_passes_when_nothing_is_open() -> None:
    assert run({"no_open_alert": True}, Gate()) == ""


def test_the_first_matching_switch_wins_and_says_which() -> None:
    """两个开关同时命中时只回一句：`suppressed` 的投递记录只有一行，理由要能读。"""
    gate = Gate(
        open_alert=OpenAlert(id="al-1", stage=HIT, on=date(2026, 10, 20)),
        last_session_on=date(2026, 10, 27),
    )
    got = run({"exclude_halt": True, "no_open_alert": True}, gate)
    assert got.startswith("该市场")  # `exclude_halt` 在 `FILTER_KEYS` 里排在前面


@pytest.mark.parametrize(
    "switch", ["time_window", "data_freshness", "exclude_halt", "no_open_alert"]
)
def test_every_declared_switch_has_an_implementation(switch: str) -> None:
    """名单来自 `core.rules.FILTER_KEYS`：这里少一个实现，那一头就能配出一个恒真的开关。"""
    assert run({switch: True}, Gate()) == ""  # 空 Gate ⇒ 全部放行，函数不抛


def test_the_gate_is_a_value_object_not_a_repo() -> None:
    """过滤器不许自己查库——否则这四个开关在没库的测试里跑不起来，而 §10.1 说评估是纯函数。"""
    import inspect

    from sams.services.rules import filters

    assert "storage" not in inspect.getsource(filters)
