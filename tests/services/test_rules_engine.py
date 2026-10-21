"""一趟求值：判定 → 过滤 → 冷却 → 投递 → 事件（spec §10.1、D94）。

时钟冻在 2026-10-20T07:30Z（北京 15:30，A 股已收盘）：冷却的判据是
「距上一条多少分钟」，用墙钟的话这枚文件会在跑到第 30 分钟边界时随机红。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from support_rules import (
    CODE,
    MARKET,
    NOW,
    TODAY,
    USER,
    FixedClock,
    Harness,
    StubCalendar,
    snap,
)

from sams.analysis.adjust import DayFactor
from sams.core.config import MarketCfg
from sams.core.errors import NotAdjustable
from sams.services.rules.engine import SESSION_LOOKBACK_DAYS
from sams.storage.repos.theses import INVALIDATED, ThesisRepo

#: 同一个 UTC 时刻的另一种读法：北京已经跨到 10-21 凌晨。
#: 「引擎问日历的是哪一天」只有这种时刻才分得清本地日与 UTC 日。
LATE_EVENING_UTC = datetime(2026, 10, 20, 20, 0, tzinfo=UTC)


@pytest.fixture
def h(db, settings) -> Harness:
    return Harness(db, settings)


# --- 投递那一条路 ------------------------------------------------------------------------

def test_a_breach_raises_one_alert_and_two_events(h) -> None:
    h.add()
    report = h.engine().check(USER)
    assert report.quiet == 0
    assert len(h.sender.sent) == 1
    message = h.sender.sent[0]
    assert message.severity == "strong"
    assert message.stage == "hit"
    assert message.value_text == "9.50 CNY"
    assert message.threshold_text == "10.00 CNY"
    assert report.raised[0].alert_id == "alert-1"
    assert [hit.stage for hit in h.hits] == ["hit"]
    assert [got.alert_id for got in h.triggered] == ["alert-1"]


def test_the_action_line_names_the_kind_and_the_users_own_name(h) -> None:
    h.add(name="别让它跌过十块")
    report = h.engine().check(USER)
    note = h.sender.sent[0].action_note
    assert "止损位到了" in note and "别让它跌过十块" in note
    assert report.raised[0].action_note == note


def test_the_body_carries_the_pnl_the_constraint_and_the_reason(h) -> None:
    h.add()
    h.snapshot = snap(qty=1000.0, avg_cost=12.0, opened_on=TODAY)
    h.engine().check(USER)
    body = h.sender.sent[0].body
    assert "浮亏 2,500.00 CNY" in body
    assert "T+1" in body  # 今日买入：这条今天执行不了
    assert "绝对价止损线 10.00 CNY" in body


def test_a_state_only_hit_still_reaches_the_sender(h) -> None:
    """状态型命中（`stop.thesis_drift` 那类只报状态、不比数值）的 `Verdict.ok` 是 False。

    这一枚是「判命中用 `hit is not None`」那道锁的唯一证据：把判据换成 `.ok`，
    这一类告警就永远不会投出去，而其余用例全是 `stop.fixed`（它的 `.ok` 恰好为 True）。
    """
    theses = ThesisRepo(h.db)
    made = theses.create(
        user_id=USER, market=MARKET, code=CODE, style="value", actor_id="u1", now=NOW
    )
    #: 论被判死、没有基线分、没有赔率 ⇒ 只有「状态」那一条成立，数值那一侧全空
    h.thesis = theses.advance(USER, made.id, INVALIDATED, actor_id="u1", now=NOW)
    assert h.thesis.baseline_score is None and h.thesis.reward_risk is None
    h.add(
        kind="stop.thesis_drift",
        name="理由没了就喊我",
        target_type="position",
        target_ref="*",
        account_id="acc-1",
        thesis_id=made.id,
        params={},
    )
    h.snapshot = snap(account_id="acc-1")
    report = h.engine().check(USER)
    assert len(report.raised) == 1
    assert report.raised[0].value_text == "—"  # 没有数值可报，那一条照样推了出去
    assert len(h.sender.sent) == 1


def test_a_cross_currency_position_shows_the_converted_number(h) -> None:
    """折 CNY 那一支的常驻用例：方向是 1 USD = 7.2 CNY（标的 → 汇总，`notes.Fx`）。

    库里只种了 USD→CNY 这一侧：参数传反 ⇒ 读的是 CNY→USD ⇒ 读不到 ⇒ 那句折合数消失，
    这一枚就红。方向写反的症状本来是「大约对」——那是最难查的错，所以它得有人常盯着。
    """
    h.add()
    h.fx.put(TODAY, "USD", "CNY", 7.2)
    h.snapshot = snap(currency="USD", report_currency="CNY", qty=100.0, avg_cost=12.0)
    h.engine().check(USER)
    body = h.sender.sent[0].body
    assert "浮亏 250.00 USD" in body
    assert "折 CNY 1,800.00" in body
    assert f"（{TODAY} 汇率 7.2）" in body


def test_a_cross_currency_position_without_a_rate_says_so(h) -> None:
    """读不到汇率时明写「没读到」，而不是把那一行悄悄省掉（省略 ⇒ 用户按标的币种加总）。"""
    h.add()
    h.snapshot = snap(currency="USD", report_currency="CNY", qty=100.0, avg_cost=12.0)
    h.engine().check(USER)
    body = h.sender.sent[0].body
    assert "浮亏 250.00 USD" in body
    assert "没读到 USD→CNY" in body
    assert "折 CNY" not in body


def test_an_above_line_price_leaves_the_run_quiet(h) -> None:
    h.add()
    h.snapshot = snap(price=11.0)
    report = h.engine().check(USER)
    assert report.raised == () and report.suppressed == ()
    assert report.skipped == () and report.quiet == 1


def test_a_missing_price_is_reported_as_unable_not_as_quiet(h) -> None:
    """「判不了」与「没到」是两句话（§1 的取向：判不准就放行，但要说）。"""
    h.add()
    h.snapshot = snap(price=None, price_on=None, price_age_days=None)
    report = h.engine().check(USER)
    assert report.raised == () and report.quiet == 0
    assert len(report.skipped) == 1
    assert "没有" in report.skipped[0].reason and "现价" in report.skipped[0].reason


def test_a_dry_run_judges_everything_and_sends_nothing(h) -> None:
    h.add()
    report = h.engine().check(USER, dry_run=True)
    assert report.dry_run is True
    assert len(report.raised) == 1
    assert report.raised[0].alert_id == ""
    assert h.sender.sent == [] and h.hits == [] and h.triggered == []


def test_an_unreadable_target_is_skipped_instead_of_killing_the_run(h) -> None:
    """挂在一个查无此码上的规则：这一条判不了，那一趟要跑完并且把原因写出来。"""
    h.read_raises = NotAdjustable("A/000001 全库一根 K 线都没有，主档里也没有这个代码")
    h.add(target_ref="A/000001")
    report = h.engine().check(USER)
    assert report.rules == 1 and report.judged == 1
    assert len(report.skipped) == 1 and report.raised == ()
    assert "K 线" in report.skipped[0].reason
    assert h.sender.sent == []


# --- 冷却（D94）--------------------------------------------------------------------------

def test_cooldown_swallows_a_repeat_of_the_same_stage(h) -> None:
    h.add()
    h.seed_hit(minutes_ago=5, stage="hit")
    report = h.engine().check(USER)
    assert h.sender.sent == []
    assert len(report.suppressed) == 1
    why = report.suppressed[0].reason
    assert "冷却" in why and "上一条" in why and "hit" in why


def test_an_escalation_punches_through_the_cooldown(h) -> None:
    """D94：`pre` 报过之后 30 分钟内的 `hit` 是真跌破，不能被降噪吞掉。"""
    h.add()
    h.seed_hit(minutes_ago=5, stage="pre")
    report = h.engine().check(USER)
    assert len(h.sender.sent) == 1
    assert report.suppressed == () and report.raised[0].stage == "hit"


def test_a_hit_beyond_the_window_reports_again(h) -> None:
    h.add()
    h.seed_hit(minutes_ago=31, stage="hit")
    assert len(h.engine().check(USER).raised) == 1


def test_a_hit_exactly_at_the_window_edge_reports(h) -> None:
    """边界：`gap >= window`，恰好 30 分钟就是窗口已过（31 与 0 钉了，这个点没钉）。"""
    h.add()
    h.seed_hit(minutes_ago=30, stage="hit")
    assert len(h.engine().check(USER).raised) == 1


def test_an_unrecognized_stage_in_the_history_punches_through(h) -> None:
    """一条脏历史不该把整趟求值打掉（与 `filters.no_open_alert` 同一条取向，T-12）。

    `alerts.stage` 那一列没有词表 CHECK，而生产里写它的那一头（`monitor/router.py`）把文本
    原样写进去：入池那类历史行的 stage 是「入池」这种词，`escalates` 认不出就抛。
    认不出上一档有多响 ⇒ 判不出来 ⇒ 穿冷却放行——冷却那句压制的话不说，这一条投出去。
    """
    h.add()
    h.db.execute(
        "INSERT INTO alerts (id, user_id, rule_id, account_id, market, code, stage,"
        " triggered_at, status, payload_json, severity)"
        " VALUES ('dirty-row', ?, ?, NULL, ?, ?, '入池', ?, 'new', '{}', 'mid')",
        [USER, h.added_id, MARKET, CODE, NOW - timedelta(minutes=5)],
    )
    report = h.engine().check(USER)
    assert len(report.raised) == 1
    assert report.suppressed == ()
    assert len(h.sender.sent) == 1


def test_the_cooldown_window_is_the_rules_own_number(h) -> None:
    """`cooldown_min = 0` 的意思是「每一次都报」（§10.1 那个默认值不是命令）。"""
    h.add(cooldown_min=0)
    h.seed_hit(minutes_ago=0, stage="hit")
    assert len(h.engine().check(USER).raised) == 1


def test_another_accounts_hit_does_not_silence_this_one(h) -> None:
    """§1 那张表的第三行：去重键含 `account_id`（§10.1）。

    两头都钉：A 账户那条拦不住 B 账户的破位；同一条 B 的才拦得住。
    只钉前一头的话，把键写成「不含 account_id」也能过——因为那时谁也拦不住谁。
    """
    h.add(account_id="acc-b")
    h.snapshot = snap(account_id="acc-b")
    h.seed_hit(minutes_ago=5, stage="hit", account_id="acc-a")
    assert len(h.engine().check(USER).raised) == 1
    h.seed_hit(minutes_ago=5, stage="hit", account_id="acc-b")
    assert h.engine().check(USER).suppressed != ()


# --- 四个开关在这一趟里的位置（§10.1）------------------------------------------------------

def test_a_switch_suppresses_and_says_which_one(h) -> None:
    h.add(filters={"data_freshness": True})
    h.snapshot = snap(price_age_days=10)
    report = h.engine().check(USER)
    assert h.sender.sent == []
    assert "10 天前" in report.suppressed[0].reason


def test_no_open_alert_suppresses_a_quieter_repeat_on_the_same_symbol(h) -> None:
    h.add(filters={"no_open_alert": True})
    h.seed_hit(minutes_ago=5, stage="urgent")
    report = h.engine().check(USER)
    assert "未处理" in report.suppressed[0].reason


def test_the_gate_asks_the_calendar_for_the_targets_own_market(h) -> None:
    """问的是谁的日历、问的是哪一天。

    时刻挑在北京已经跨日、UTC 还没跨日的那一格（10-20T20:00Z = 北京 10-21 04:00）：
    拿 UTC 日去问日历的话，回走的起点会是 10-20，这一枚就红。
    """
    h.add()
    h.clock = FixedClock(LATE_EVENING_UTC)
    h.engine().check(USER)
    assert h.calendar.day_calls, "引擎一次日历都没问：三个要日历的开关全在读空气"
    assert {market for market, _ in h.calendar.day_calls} == {MARKET}
    probed = [day for _, day in h.calendar.day_calls]
    #: 起点是**标的市场本地的那一天**，之后逐日往回退（`_last_session` 的回走）
    assert probed[0] == TODAY + timedelta(days=1)
    assert probed == [probed[0] - timedelta(days=n) for n in range(len(probed))]


def test_the_last_session_is_walked_back_from_the_local_day(h) -> None:
    """日历一个交易日都没给 ⇒ `exclude_halt` 不拦（判不了就放行），这里钉它确实回走过。"""
    h.add()
    h.calendar = StubCalendar(open_days=())  # 日历说这些天全闭
    h.engine().check(USER)
    assert len(h.calendar.day_calls) >= SESSION_LOOKBACK_DAYS


# --- 一趟的形状：一条坏规则不拖整列（§1 第五行）--------------------------------------------

def test_a_wild_kind_in_the_db_is_skipped_and_the_run_finishes(h) -> None:
    """库里躺着这一层没实现的 kind：判不了 ⇒ 说这一路（而不是整趟崩）。

    取向是「已经躺在库里的那种行不该把整趟拖崩」，而检查必须排在 `template_for` 之前：
    kind 的名单有两份（`TEMPLATES` 在 core、`TABLE` 在这里），原本野 kind 会先在
    `template_for` 那里抛出去——抛出去就成了一句话不说清自己。
    """
    good = h.add(name="好的那条")
    h.db.execute(
        "INSERT INTO rules (id, user_id, name, enabled, target_type, target_ref,"
        " kind, condition_kind, params_json, filters_json, cooldown_min, severity,"
        " channel_ids, created_at, updated_at)"
        " VALUES ('wild-row', ?, '没实现的那种', TRUE, 'symbol', ?, 'stop.lunar',"
        " 'template', '{}', '{}', 30, 'mid', '[]', ?, ?)",
        [USER, f"{MARKET}/{CODE}", NOW, NOW],
    )
    report = h.engine().check(USER)
    assert [got.rule_id for got in report.raised] == [good.id]
    assert len(report.skipped) == 1
    assert report.skipped[0].rule_id == "wild-row"
    assert "求值实现" in report.skipped[0].reason


def test_a_broken_timezone_in_the_config_does_not_kill_the_run(h) -> None:
    """坏时区名走的是 `MarketCfg.zone()`：它把 `ZoneInfoNotFoundError` 转成 `ValidationError`。

    引擎自己 `ZoneInfo(...)` 的那一支只接 `ValidationError`，而配置里一个拼错的时区名抛的是
    `KeyError` 的子类 ⇒ 整趟崩。这里钉的是「配置写错时这一条照样投出去」。
    """
    h.add()
    h.settings = replace(
        h.settings,
        markets={"A": MarketCfg("A", "Mars/Phobos", "CNY", 30, ("akshare",))},
    )
    report = h.engine().check(USER)
    assert len(report.raised) == 1
    assert len(h.sender.sent) == 1


def test_a_rule_that_cannot_expand_blocks_only_itself(h) -> None:
    good = h.add(name="好的那条")
    # 绕过仓库直接写一行「持仓类却没有账户」：仓库那道闸拦的是写入，这一枚要的是库里
    # 已经躺着这种行时（M3-D 的派生写入方、手工修过库）一趟照旧出别的面
    h.db.execute(
        "INSERT INTO rules (id, user_id, name, enabled, target_type, target_ref,"
        " kind, condition_kind, params_json, filters_json, cooldown_min, severity,"
        " channel_ids, created_at, updated_at)"
        " VALUES ('bad-row', ?, '缺账户的那条', TRUE, 'position', '*', 'stop.pct',"
        " 'template', '{\"pct\": 0.08}', '{}', 30, 'strong', '[]', ?, ?)",
        [USER, NOW, NOW],
    )
    report = h.engine().check(USER)
    assert [got.rule_id for got in report.raised] == [good.id]
    assert len(report.blocked) == 1
    assert "account_id" in report.blocked[0].reason


def test_disabled_rules_are_skipped_unless_named(h) -> None:
    made = h.add(enabled=False)
    assert h.engine().check(USER).raised == ()
    assert len(h.engine().check(USER, rule_id=made.id).raised) == 1


def test_as_of_moves_the_reading_day_without_moving_the_clock(h) -> None:
    h.add()
    report = h.engine().check(USER, as_of=date(2026, 10, 16))
    assert report.as_of == date(2026, 10, 16)
    assert len(report.raised) == 1


# --- 除权换算（§10.2 在引擎这一头的四个形状）----------------------------------------------

#: 10 派 1、前收 10.00 ⇒ 因子 10/9，写在它之前的「10 元」今天站在 9.00。
#: 数与 `test_rules_anchor.py` 那一枚同一个来源：券商在除权日调整成本价是同一个动作。
DIVIDEND = DayFactor(
    ex_date=date(2024, 10, 26), prev_close=10.0, ex_ref=9.0, factor=10.0 / 9.0, cash=1.0
)
#: `bars_from` 比 `params_on` 早，中间那段才是完整的：搬得起
MOVED = {"factors": (DIVIDEND,), "bars_from": date(2024, 1, 1)}
STAMPED = date(2024, 3, 1)


def test_the_line_is_moved_before_it_is_judged(h) -> None:
    """除权那天不许误报跌破止损：规则开着、现价低于人填的那条线，就是不响。

    搬之前 10.00 > 9.50 ⇒ 响；搬之后 9.00 < 9.50 ⇒ 安静。这一枚与下一枚合起来是 §10.2
    那句话的两个方向——少搬会误报，搬过头会漏报。
    """
    h.snapshot = snap(**MOVED)
    h.add(params_on=STAMPED)
    report = h.engine().check(USER)

    assert report.raised == () and report.quiet == 1
    assert h.sender.sent == []


def test_the_same_rule_still_fires_when_the_calendar_is_empty(h) -> None:
    """同一份参数、同一条线，库里那段没有除权 ⇒ 照旧响，阈值还是写着的那一个。

    这一枚是上一枚的对照组。少了它，「不响」可能来自任何一个别的原因（现价读不到、
    过滤拦了、冷却挡了），而 §10.2 要钉的是「除权这一件事造成的差别」。
    """
    h.add(params_on=STAMPED)
    report = h.engine().check(USER)

    assert len(report.raised) == 1
    assert h.sender.sent[0].threshold_text == "10.00 CNY"


def test_a_line_without_a_date_stamp_is_skipped_and_names_the_command(h) -> None:
    """有除权、没有锚 ⇒ 整条不判，落进 skipped，并且说得出下一步跑什么。

    用例走的是仓库（`h.add` → `RuleRepo.create`），不是维护面，所以那一行没有日期戳——
    这正是 0017 之前建的那些行的形状。拿原值判是误报，搬又怕搬少了。
    """
    h.snapshot = snap(**MOVED)
    h.add()
    report = h.engine().check(USER)

    assert report.raised == () and len(report.skipped) == 1
    assert report.judged == 1
    assert "rule edit" in report.skipped[0].reason


def test_a_move_shows_up_in_the_body_and_the_event(h) -> None:
    """搬过的数自带出处：正文那一行说清跨了哪天，事件那份 payload 给的是数不是句子。

    线 11.00 搬到 9.90，现价 9.50 ⇒ 仍然响。这一枚同时钉住「搬过头也不漏报」的反面：
    9.90 比 11.00 低，而 9.50 还在它下面，报出来的阈值必须是搬完的那一个。
    """
    h.snapshot = snap(**MOVED)
    h.add(params={"price": 11.0}, params_on=STAMPED)
    report = h.engine().check(USER)

    assert len(report.raised) == 1
    message = h.sender.sent[0]
    assert message.threshold_text == "9.90 CNY"
    assert "换算" in message.body and "2024-10-26" in message.body
    moved = h.hits[0].payload["moved"]
    assert moved[0]["column"] == "params.price"
    assert moved[0]["after"] == pytest.approx(9.9)
    assert moved[0]["on"] == "2024-03-01"
    assert moved[0]["ex_dates"] == ["2024-10-26"]


def test_a_row_missing_its_param_is_reported_not_a_crash(h) -> None:
    """库里那一行压根没有这个键（老模板写的）：报「缺参数」，别让整趟崩。

    搬运跑在求值之前，所以它若不认「键不在」这一种缺，`KeyError` 会一路抛出 `check`——
    `_verdict` 那张网本来接的就是这种行，而 `check` 外面没有兜底的 `try`：一条脏行会
    让屏幕上其他那几条一起消失。
    """
    h.snapshot = snap(**MOVED)
    h.db.execute(
        "INSERT INTO rules (id, user_id, name, enabled, target_type, target_ref,"
        " kind, condition_kind, params_json, filters_json, cooldown_min, severity,"
        " channel_ids, params_on, created_at, updated_at)"
        " VALUES ('no-price', ?, '键都没有的那条', TRUE, 'symbol', ?, 'stop.fixed',"
        " 'template', '{}', '{}', 30, 'strong', '[]', ?, ?, ?)",
        [USER, f"{MARKET}/{CODE}", STAMPED, NOW, NOW],
    )
    report = h.engine().check(USER)

    assert report.raised == () and len(report.skipped) == 1
    assert "那一行是老模板写的" in report.skipped[0].reason


def test_two_gaps_report_both_commands(h) -> None:
    """两个缺口同时存在 ⇒ 一条结论里两句都带上，各自那条下一步命令都不许掉。

    屏幕上「判不了」一行就是一条规则（`_print_check`），所以两段合成一条而不是两行；
    只报第一个的话，人跑完 `ledger buy` 要等下一趟才看见第二句，而 D55 要的是当场说全。
    """
    h.snapshot = snap(
        **MOVED,
        avg_cost=12.0,
        opened_on=None,
        target_price=20.0,
        target_on=None,
    )
    h.add(
        kind="tp.tiered",
        target_type="position",
        target_ref="*",
        account_id="sim-1",
        # 这一种只认 `tiers`（有默认值），`add` 那份默认的 `price` 它不认
        params={},
        params_on=STAMPED,
    )
    report = h.engine().check(USER)

    assert report.raised == () and len(report.skipped) == 1
    reason = report.skipped[0].reason
    assert "ledger buy" in reason and "thesis revise" in reason
