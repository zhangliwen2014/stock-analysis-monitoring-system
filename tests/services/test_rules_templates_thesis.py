"""读论的那几条：`stop.thesis_drift`、`tp.tiered`、`price.cross_*`、`thesis.*`、`scheduled.*`。

与止损那文件的区别只有一件事——这里每一枚都可能因为「论没读到」而判不了，
而判不了必须在 `why_not` 里有话（§1 那张表：屏幕上「没触发」与「从没比过」长得一样）。

`thesis` 用的是仓库那个真 `Thesis`，不是手搓的替身：字段名写错时这里就该红。
"""

from __future__ import annotations

from datetime import date

from sams.core.rules import HIT
from sams.services.rules.snapshot import Snapshot
from sams.services.rules.templates import Ask, stop_thesis_drift
from sams.storage.repos.theses import HIT_STOP, INVALIDATED, TRACKING, Thesis


def snap(**over) -> Snapshot:
    base = {
        "market": "A",
        "code": "601088",
        "name": "中国神华",
        "currency": "CNY",
        "price": 10.0,
        "price_on": date(2026, 10, 20),
        "prev_close": 10.2,
        "avg_cost": 12.0,
        "qty": 100.0,
    }
    base.update(over)
    return Snapshot(**base)


def thesis(**over) -> Thesis:
    base = {
        "id": "th-1",
        "user_id": "u-1",
        "market": "A",
        "code": "601088",
        "style": "value",
        "status": TRACKING,
        "stop_price": 9.0,
        "target_price": 15.0,
        "reward_risk": 2.5,
        "baseline_score": 60.0,
    }
    base.update(over)
    return Thesis(**base)


_MISSING = object()


def ask(params: dict, snapshot_over: dict, the=_MISSING) -> Ask:
    """`the` 不传 ⇒ 造一条活着的论；显式传 `None` ⇒ 「这一趟没读到论」。

    这两件事必须分开：`the=None` 走 `the or thesis()` 会悄悄换成默认那条论，
    而「没读到论」正是 `test_an_unread_thesis...` 要验的那一条。
    """
    return Ask(
        snapshot=snap(**snapshot_over),
        params=params,
        thesis=thesis() if the is _MISSING else the,
    )


DRIFT = {}  # 这一条无参数：`validate_params` 补完就是 {}


def test_a_dead_thesis_is_drift_even_when_the_price_never_moved() -> None:
    """§10.2 那一行「价格没跌但理由已失效」：现价 10 高于止损线，论却已被判死。"""
    got = stop_thesis_drift(ask(DRIFT, {}, the=thesis(status=INVALIDATED)))
    assert got.hit is not None
    assert got.hit.stage == HIT
    assert INVALIDATED in got.hit.why


def test_a_score_drop_across_a_tier_is_named_with_both_numbers() -> None:
    got = stop_thesis_drift(ask(DRIFT, {"score": 35.0}))  # 60 → 35：3 档 → 1 档
    assert got.hit is not None
    assert "60" in got.hit.why and "35" in got.hit.why
    assert got.hit.threshold == 60.0


def test_a_score_wobble_inside_one_tier_is_not_drift() -> None:
    """78 → 61 一档都没掉（都是 3 档）：分项分自带窗口噪声，跨档才是能被说清的变化。

    这一枚必须同时给一个**没崩的赔率**（2.6 ≥ 建仓时的 2.5）：不给的话它落到「判不了」
    那一支，测试会因为错误的原因通过。
    """
    got = stop_thesis_drift(
        ask(DRIFT, {"score": 61.0, "odds": 2.6}, the=thesis(baseline_score=78.0))
    )
    assert got.hit is None
    assert got.why_not == ""


def test_collapsed_odds_is_a_reason_by_itself() -> None:
    got = stop_thesis_drift(ask(DRIFT, {"odds": 1.1}))  # 建仓时 2.5 倍
    assert got.hit is not None
    assert "赔率" in got.hit.why
    assert got.hit.threshold == 2.5


def test_all_three_findings_are_printed_in_one_sentence() -> None:
    got = stop_thesis_drift(
        ask(DRIFT, {"score": 20.0, "odds": 0.8}, the=thesis(status=INVALIDATED))
    )
    assert got.hit is not None
    assert got.hit.why.count("；") == 2
    assert "复核" in got.hit.why  # §9.3：只提示，不替用户平仓


def test_a_live_thesis_with_a_fresh_score_reports_nothing() -> None:
    got = stop_thesis_drift(ask(DRIFT, {"score": 62.0, "odds": 2.6}))
    assert got.hit is None
    assert got.why_not == ""


def test_an_unread_thesis_is_refused_rather_than_judged_alive() -> None:
    """规则挂着 `needs_thesis` 却没读到论 ⇒ 这是取数出了问题，不是「理由还在」。"""
    got = stop_thesis_drift(ask(DRIFT, {}, the=None))
    assert got.hit is None
    assert "论" in got.why_not


def test_a_baseline_without_a_new_score_is_a_gap_not_a_pass() -> None:
    """有基线分却合成出新分数失败 ⇒ 不许默默放过，那正是「从没比过」。"""
    got = stop_thesis_drift(
        ask(DRIFT, {"score": None, "odds": None}, the=thesis(reward_risk=None))
    )
    assert got.hit is None
    assert "判不了" in got.why_not


def test_a_stopped_out_thesis_is_drift_too() -> None:
    got = stop_thesis_drift(ask(DRIFT, {}, the=thesis(status=HIT_STOP)))
    assert got.hit is not None
    assert HIT_STOP in got.hit.why


def test_a_single_tier_drop_is_enough() -> None:
    """60 → 40 只掉一档，仍然是掉档：判据不是「掉两档」。"""
    got = stop_thesis_drift(ask(DRIFT, {"score": 40.0}))
    assert got.hit is not None
    assert "掉到 40" in got.hit.why


def test_a_thesis_without_a_baseline_is_a_gap_not_a_pass() -> None:
    """没有基线分就判不了掉档：静默会把「从没比过」显示成「没触发」。"""
    got = stop_thesis_drift(
        ask(DRIFT, {"score": 62.0, "odds": 2.6}, the=thesis(baseline_score=None))
    )
    assert got.hit is None
    assert "基线" in got.why_not


def test_a_thesis_without_a_recorded_odds_is_a_gap_not_a_pass() -> None:
    got = stop_thesis_drift(ask(DRIFT, {"score": 62.0}, the=thesis(reward_risk=None)))
    assert got.hit is None
    assert "赔率" in got.why_not


def test_two_gaps_are_both_named_not_collapsed_into_one() -> None:
    """基线与赔率同时缺数时两句都要说：只报一句会让另一半的「从没比过」继续隐身。"""
    got = stop_thesis_drift(
        ask(DRIFT, {"score": None, "odds": None}, the=thesis(reward_risk=2.5))
    )
    assert got.hit is None
    assert "掉档" in got.why_not and "赔率" in got.why_not
