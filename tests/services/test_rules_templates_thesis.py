"""读论的那几条：`stop.thesis_drift`、`tp.tiered`、`price.cross_*`、`thesis.*`、`scheduled.*`。

与止损那文件的区别只有一件事——这里每一枚都可能因为「论没读到」而判不了，
而判不了必须在 `why_not` 里有话（§1 那张表：屏幕上「没触发」与「从没比过」长得一样）。

`thesis` 用的是仓库那个真 `Thesis`，不是手搓的替身：字段名写错时这里就该红。
"""

from __future__ import annotations

import inspect
from datetime import date

import pytest

from sams.analysis.valuation.scoring import item_label
from sams.core.rules import HIT, PRE, RULE_KINDS
from sams.services.rules.snapshot import Snapshot
from sams.services.rules.templates import (
    TABLE,
    Ask,
    price_cross_above,
    price_cross_below,
    scheduled_reevaluate,
    stop_thesis_drift,
    thesis_drift,
    thesis_odds_below,
    tp_tiered,
)
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


def test_a_halfway_gain_is_a_pre_warning_with_the_tier_named() -> None:
    """成本 12 → 目标 15 这一段走了 60%：报「到 50% 那一档」，级别是 `pre`。"""
    got = tp_tiered(
        ask({"tiers": [0.5, 1.0]}, {"price": 13.8, "target_price": 15.0})
    )
    assert got.hit is not None
    assert got.hit.stage == PRE
    assert "50%" in got.hit.why
    assert got.hit.value == pytest.approx((13.8 - 12.0) / (15.0 - 12.0))


def test_the_target_price_itself_is_a_hit_not_a_pre() -> None:
    """最后一档必须是 `hit`：同一去重键上只有更响的一档能穿冷却（D94），
    两档都记 `hit` 的话，到了目标价那一条会被半小时前的 50% 吞掉。"""
    got = tp_tiered(ask({"tiers": [0.5, 1.0]}, {"price": 15.2, "target_price": 15.0}))
    assert got.hit is not None
    assert got.hit.stage == HIT
    assert "已到最后一档" in got.hit.why
    # 现价 15.2 已在目标价 15.0 上方 ⇒ 那句「走完 106.7%」得说清它不是算错
    assert "已越过目标价" in got.hit.why


def test_only_the_highest_reached_tier_is_named_when_several_are_behind_us() -> None:
    """三档走到 60% ⇒ 报 50% 那一档，而不是 30% 那一档，也不是两档各报一次。

    「只报最高一档」是这句决定的实现（一趟求值发两条同键告警会被冷却吃掉第二条），
    而上面那几枚用例每枚最多只到一档，钉不住这里：档位列表给三个，进度卡在中间。
    """
    got = tp_tiered(
        ask({"tiers": [0.3, 0.5, 1.0]}, {"price": 13.8, "target_price": 15.0})
    )  # 进度 0.6 ⇒ 越过 30% 和 50%，没到 100%
    assert got.hit is not None
    assert got.hit.threshold_text == "50%"
    assert got.hit.stage == PRE
    assert "还剩 1 档" in got.hit.why
    assert "不替你平仓" in got.hit.why  # §9.3：分批止盈也是「卖」的提醒，同样只提示


def test_a_progress_that_rounds_to_exactly_a_tier_is_not_lost_to_float() -> None:
    """成本 11.3 → 目标 15.9 走到 13.6：那是不折不扣的一半，除法却给出 0.49999…

    按字面 `>=` 判的话第一档在精确点位上哑掉，而这一条的意义就是「走到那一档要说一声」。
    """
    got = tp_tiered(
        ask({"tiers": [0.5, 1.0]}, {"price": 13.6, "avg_cost": 11.3, "target_price": 15.9})
    )
    assert got.hit is not None
    assert got.hit.threshold_text == "50%"
    assert got.hit.stage == PRE


def test_a_last_tier_below_one_does_not_claim_the_target_price_was_reached() -> None:
    """末档填 0.5 ⇒ 走完最后一档是真的，「到目标价」是假的：`validate_params` 不要求末档为 1。"""
    got = tp_tiered(
        ask({"tiers": [0.3, 0.5]}, {"price": 13.8, "target_price": 15.0})
    )  # 进度 0.6 ⇒ 已过末档 0.5
    assert got.hit is not None
    assert got.hit.stage == HIT
    assert "已到最后一档" in got.hit.why
    assert "⇒ 目标价" not in got.hit.why


def test_a_gain_below_the_first_tier_says_nothing() -> None:
    got = tp_tiered(ask({"tiers": [0.5, 1.0]}, {"price": 12.6, "target_price": 15.0}))
    assert got.hit is None
    assert got.why_not == ""


def test_a_target_at_or_below_cost_is_refused_as_a_scale_that_cannot_be_divided() -> None:
    got = tp_tiered(ask({"tiers": [0.5, 1.0]}, {"price": 10.0, "target_price": 12.0}))
    assert got.hit is None
    assert "成本" in got.why_not


def test_no_target_price_is_named_instead_of_defaulting_to_the_price() -> None:
    """拿现价当目标价 ⇒ 进度恒为 1 ⇒ 每趟都报「到了目标价」。"""
    got = tp_tiered(ask({"tiers": [0.5, 1.0]}, {"target_price": None}))
    assert got.hit is None
    assert "目标价" in got.why_not


def test_cross_below_needs_the_previous_close_to_call_it_a_cross() -> None:
    got = price_cross_below(ask({"price": 10.5}, {"prev_close": None}))
    assert got.hit is None
    assert "前收盘" in got.why_not


def test_a_price_already_below_the_line_at_the_previous_close_is_not_a_cross() -> None:
    """这就是那一枚防刷屏的用例：昨天就在下面，今天不算「穿」。

    只用现价判的话，这条规则每个交易日收盘后都响一次，而 §10.1 的去重键里没有
    「第几天」这一维——刷屏会在三天内把用户对该通道的信任磨光。
    """
    got = price_cross_below(ask({"price": 10.5}, {"prev_close": 10.4, "price": 10.1}))
    assert got.hit is None


def test_a_downward_cross_is_reported_with_both_sides_and_the_currency() -> None:
    got = price_cross_below(ask({"price": 10.5}, {"prev_close": 10.6, "price": 10.1}))
    assert got.hit is not None
    assert got.hit.stage == HIT
    assert got.hit.threshold_text == "10.50 CNY"
    assert "前收盘" in got.hit.why and "现价" in got.hit.why


def test_cross_above_is_the_mirror_image() -> None:
    got = price_cross_above(ask({"price": 10.5}, {"prev_close": 10.4, "price": 10.8}))
    assert got.hit is not None
    assert got.hit.stage == HIT
    assert got.hit.threshold == 10.5


def test_cross_above_stays_quiet_when_we_were_already_above() -> None:
    got = price_cross_above(ask({"price": 10.5}, {"prev_close": 10.9, "price": 11.0}))
    assert got.hit is None


def test_item_label_names_the_known_items_and_falls_back_to_the_key() -> None:
    """屏幕上要写「市赚率 PR」而不是 `pr`：键是身份，标签是给人读的词。

    这个函数是 T-11 唯一碰 `analysis` 的地方，而它存在的原因就是「不许在 `templates.py`
    里再抄一份 `_ITEM_LABELS`」——两份名单必然有一天不一样。
    """
    assert item_label("pr") == "市赚率 PR"
    assert item_label("unknown_key") == "unknown_key"


def test_odds_below_reports_with_the_multiplier_unit() -> None:
    got = thesis_odds_below(ask({"threshold": 1.5}, {"odds": 1.1}))
    assert got.hit is not None
    assert got.hit.stage == HIT
    assert got.hit.threshold_text == "1.5 倍"
    assert got.hit.value_text == "1.1 倍"


def test_odds_at_the_threshold_is_not_below_it() -> None:
    got = thesis_odds_below(ask({"threshold": 1.5}, {"odds": 1.5}))
    assert got.hit is None
    assert got.why_not == ""


def test_odds_a_hair_under_the_threshold_is_still_not_below_it() -> None:
    """±1e-9 容差在这里同样成立：赔率是 (目标 − 现价) / (现价 − 止损) 除出来的，
    「真值恰好 1.5」的那一笔读回来可能是 1.4999999999999996——阈值处差一个浮点
    噪声不该触发，而真跌破（下面 1e-6 那半枚）仍要触发。"""
    quiet = thesis_odds_below(ask({"threshold": 1.5}, {"odds": 1.5 - 1e-12}))
    assert quiet.hit is None
    assert quiet.why_not == ""
    lit = thesis_odds_below(ask({"threshold": 1.5}, {"odds": 1.5 - 1e-6}))
    assert lit.hit is not None


def test_a_missing_odds_is_never_read_as_zero() -> None:
    """赔率算不出来时不许当 0：0 < 1.5 ⇒ 数据一缺，所有 odds 规则全体触发。"""
    got = thesis_odds_below(ask({"threshold": 1.5}, {"odds": None}))
    assert got.hit is None
    assert "赔率" in got.why_not


def test_a_dropped_item_is_named_by_label_with_both_scores() -> None:
    got = thesis_drift(
        ask(DRIFT, {"items": (("pr", 40.0), ("roe", 80.0))},
            the=thesis(baseline_items=(("pr", 80.0), ("roe", 80.0)), baseline_score=60.0))
    )
    assert got.hit is not None
    assert "市赚率 PR 80 → 40" in got.hit.why
    # 没掉的那一项不许出现在句子里——用它的**标签**断，而不是键名：
    # 实现把未掉项也拼进去时拼的是「净资产收益率」，断 `"roe" not in` 照样过。
    assert "净资产收益率" not in got.hit.why


def test_an_item_that_barely_moved_within_a_tier_is_not_a_drop() -> None:
    """分项分的刻度顶在 20 的整倍数上：100.0 与 100.0-1e-14 分属 5 档与 4 档。

    只比档位的话，浮点噪声会发出一条「100 → 100（5 档 → 4 档）」的自相矛盾告警。
    """
    got = thesis_drift(
        ask(DRIFT, {"items": (("pr", 100.0 - 1e-14),), "score": 100.0},
            the=thesis(baseline_items=(("pr", 100.0),), baseline_score=60.0))
    )
    assert got.hit is None
    assert got.why_not == ""


def test_a_total_baseline_without_items_is_still_never_having_compared() -> None:
    """只有 `baseline_score`、分项为空 ⇒ 还是「从没比过」：`set_baseline` 收得下这种论。

    这一枚钉的是守卫别写成「两个都缺才算没基线」——那样这条规则会在该论上永远沉默，
    而屏幕上读起来是「观点未漂移」。
    """
    got = thesis_drift(
        ask(DRIFT, {"items": (("pr", 40.0),), "score": 40.0},
            the=thesis(baseline_items=(), baseline_score=60.0))
    )
    assert got.hit is None
    assert "基线" in got.why_not


def test_a_drift_sentence_asks_for_a_review_not_a_close() -> None:
    got = thesis_drift(
        ask(DRIFT, {"items": (("pr", 20.0),)},
            the=thesis(baseline_items=(("pr", 80.0),), baseline_score=60.0))
    )
    assert got.hit is not None
    assert "复核" in got.hit.why and "不替你平仓" in got.hit.why


def test_no_baseline_is_named_as_never_having_compared() -> None:
    """D96：从没比过 ≠ 没漂移。这一枚断言的就是那句谎不许被说出来。"""
    got = thesis_drift(
        ask(DRIFT, {"items": (("pr", 40.0),), "score": 40.0},
            the=thesis(baseline_items=(), baseline_score=None))
    )
    assert got.hit is None
    assert "基线" in got.why_not


def test_an_item_that_was_not_composed_this_time_is_a_gap_not_a_pass() -> None:
    got = thesis_drift(
        ask(DRIFT, {"items": (("roe", 80.0),)},
            the=thesis(baseline_items=(("pr", 80.0), ("roe", 80.0)), baseline_score=60.0))
    )
    assert got.hit is None
    assert "市赚率 PR" in got.why_not


def test_a_fresh_disclosure_is_a_reevaluate_reminder() -> None:
    got = scheduled_reevaluate(
        ask({"within_days": 5}, {"disclosed_on": date(2026, 10, 18)})
    )  # price_on = 2026-10-20 ⇒ 2 天前
    assert got.hit is not None
    assert got.hit.stage == HIT
    assert "复核" in got.hit.why


def test_an_old_disclosure_is_silent() -> None:
    got = scheduled_reevaluate(
        ask({"within_days": 5}, {"disclosed_on": date(2026, 8, 1)})
    )
    assert got.hit is None
    assert got.why_not == ""


def test_a_disclosure_date_in_the_future_is_refused_not_reported() -> None:
    """披露日晚于这一趟的基准日 ⇒ 那是脏数据，报出来是一条来自未来的提醒。"""
    got = scheduled_reevaluate(
        ask({"within_days": 5}, {"disclosed_on": date(2026, 11, 1)})
    )
    assert got.hit is None
    assert "未来" in got.why_not


def test_a_disclosure_on_the_last_day_of_the_window_is_still_in_it() -> None:
    """`age == within` 在窗口内：写成 `>=` 的话窗口静默少一天，而全绿的测试读不出这件事。"""
    got = scheduled_reevaluate(
        ask({"within_days": 5}, {"disclosed_on": date(2026, 10, 15)})
    )  # price_on = 2026-10-20 ⇒ 整 5 天
    assert got.hit is not None
    assert got.hit.value_text == "5 天前"


def test_a_disclosure_one_day_past_the_window_is_silent() -> None:
    got = scheduled_reevaluate(
        ask({"within_days": 5}, {"disclosed_on": date(2026, 10, 14)})
    )
    assert got.hit is None
    assert got.why_not == ""


def test_both_missing_dates_are_named_rather_than_the_first_one_found() -> None:
    """两个都缺时只报披露日 ⇒ 用户去修基本面链，而真因是这一趟没有价。"""
    got = scheduled_reevaluate(
        ask({"within_days": 5}, {"disclosed_on": None, "price_on": None})
    )
    assert got.hit is None
    assert "披露" in got.why_not and "基准日" in got.why_not


def test_no_disclosure_date_names_the_missing_input() -> None:
    got = scheduled_reevaluate(ask({"within_days": 5}, {"disclosed_on": None}))
    assert got.hit is None
    assert "披露" in got.why_not


def test_the_table_covers_every_declared_kind() -> None:
    """少一个 kind 就是那条规则永远不会被求值：`RuleRepo` 收得下它，引擎只会沉默。

    这一枚是 T-7 许下的那个承诺的兑现（「`TABLE` 在 T-8 ~ T-11 里逐段补全」）。
    """
    assert set(TABLE) == set(RULE_KINDS)


def test_every_table_entry_takes_exactly_one_argument() -> None:
    """签名单方面收一个 `ask`：写成两个参数的话这一枚要等到引擎求值才 `TypeError`。

    名字检查（`kind.replace(".", "_")`）钉的是「键与函数对得上」， arity 钉的是「能被
    `TABLE[kind](ask)` 调起来」——这两件事各自会漏不同的错。
    """
    for kind, fn in TABLE.items():
        assert fn.__name__ == kind.replace(".", "_"), kind
        params = list(inspect.signature(fn).parameters)
        assert params == ["ask"], f"{kind} 的签名是 {params}，引擎只会按 `fn(ask)` 调它"
