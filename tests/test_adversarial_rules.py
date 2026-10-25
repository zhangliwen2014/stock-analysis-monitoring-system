"""跨层的说谎路径（spec §10 的交界面）。

这里每一条都在单元用例的射程之外：每一层的用例只验自己那一层，而这几条要的是两层接上之后
「屏幕上那句话与库里那一行是不是同一件事」：缺参数与投递失败也都折进 skipped，
每条各钉一枚，钉的就是那一句会不会说谎。
"""

from __future__ import annotations

import pytest
from support_rules import CODE, MARKET, USER, Harness
from support_valuation import make_container

from sams.core.errors import StorageError

OTHER = "u-somebody-else"
ANOTHER_CODE = "600000"


@pytest.fixture
def h(db, settings) -> Harness:
    return Harness(db, settings)


@pytest.fixture
def container(db, settings):
    """真 `RuleData` 那一头用的容器：这一枚只要自选与规则两张表，行情一律不铺。"""
    return make_container(db, settings, with_bars=False, with_fundamentals=False)


# --- 冷却：判据是库里那一行，不是内存 ------------------------------------------------------


def test_a_second_engine_on_the_same_db_does_not_resend(h) -> None:
    """§10.1 那句「进程重启从 `alerts` 表恢复冷却状态，不重复推送」。

    这一枚必须配 `StoringSender`：替身不写库的话，第二台引擎什么也读不到，用例就退化成
    「手工种一行」——那半条链没人验过。
    """
    h.add()
    assert len(h.storing().check(USER).raised) == 1
    report = h.storing().check(USER)
    assert report.raised == ()
    assert len(report.suppressed) == 1
    assert "冷却" in report.suppressed[0].reason


def test_a_dry_run_leaves_nothing_behind_for_the_next_run(h) -> None:
    """试运行先判后跳过投递，所以库里那一行也没写：下一趟真跑时不该被自己挡掉。"""
    h.add()
    assert h.storing().check(USER, dry_run=True).dry_run is True
    assert h.alerts.list_for(USER) == []
    assert len(h.storing().check(USER).raised) == 1


def test_two_rules_on_one_symbol_do_not_share_a_cooldown(h) -> None:
    """去重键含 `rule_id`：甲说过不等于乙也说过，否则第二条规则生下来就是静音的。"""
    first = h.add(name="甲")
    second = h.add(name="乙")
    h.seed_hit(rule_id=first.id, minutes_ago=5)
    report = h.engine().check(USER)
    assert [item.rule_id for item in report.raised] == [second.id]
    assert [item.rule_id for item in report.suppressed] == [first.id]


def test_deleting_a_rule_does_not_mute_its_replacement(h) -> None:
    """删规则只删规则：那一行是忽略率的唯一输入，而新规则有自己的 id，不该继承旧冷却。"""
    first = h.add()
    h.storing().check(USER)
    assert len(h.alerts.list_for(USER)) == 1
    assert h.rules.remove(first.id, user_id=USER) is True
    assert len(h.alerts.list_for(USER)) == 1
    h.add(name="跌破 10 元（重建）")
    assert len(h.storing().check(USER).raised) == 1


def test_a_failed_delivery_still_counts_as_said(h) -> None:
    """这一枚钉的是「我们选了什么」，不是「我们对的地方」：通道 503 之后不会立刻重发。

    冷却读的是 `alerts` 那一行，不是 `notify_log.outcome`。重发已经在那一头做过了
    （`NotifyService` 三次 + sleep）。要让「失败」也算没说，改的是 `AlertRepo.latest_for`
    的判据而不是引擎——那时这一枚要跟着翻，别把它当漏网之鱼删掉。
    """
    rule = h.add()
    said = h.seed_hit(rule_id=rule.id, minutes_ago=5)
    h.alerts.record(said.id, "c-1", "failed", error="通道 503")
    report = h.engine().check(USER)
    assert report.raised == ()
    assert "冷却" in report.suppressed[0].reason


# --- 两道闸各自独立：no_open_alert 与冷却 ---------------------------------------------------


def test_handling_the_open_alert_unmutes_the_next_run(h) -> None:
    """`no_open_alert` 必须有一个解除的出口，否则它是一个静音开关。

    解除它的那一屏是 `sams alert handle`：这一枚是它另一头的验收。

    种下去的那条推到 31 分钟前：5 分钟前的那条同时挡两道闸（冷却与未处理），
    按掉之后第二趟仍被冷却挡着，红了会去查错的地方。
    """
    rule = h.add(filters={"no_open_alert": True})
    open_row = h.seed_hit(rule_id=rule.id, minutes_ago=31)
    assert "未处理" in h.engine().check(USER).suppressed[0].reason
    h.alerts.act(open_row.id, user_id=USER, action_kind="other", note="已挂条件单")
    assert len(h.engine().check(USER).raised) == 1


def test_dismissing_unmutes_and_keeps_the_reason_for_the_ignore_rate(h) -> None:
    """忽略也是解除：那一列留下来了，静音才不是「按掉就再也看不见」。"""
    rule = h.add(filters={"no_open_alert": True})
    open_row = h.seed_hit(rule_id=rule.id, minutes_ago=31)
    h.alerts.dismiss(open_row.id, user_id=USER, code="misreport", reason="除权造成的假跌破")
    assert len(h.engine().check(USER).raised) == 1
    kept = h.alerts.get(open_row.id)
    assert kept.dismiss_reason == "除权造成的假跌破"
    assert kept.handle_note is None


def test_another_users_open_alert_does_not_mute_this_one(h) -> None:
    """`latest_open` 按用户分：甲没点掉的那条，不该让乙的止损永远不响。"""
    h.add(filters={"no_open_alert": True})
    h.seed_hit(user_id=OTHER, rule_id="r-elsewhere")
    assert len(h.engine().check(USER).raised) == 1


def test_an_open_alert_on_another_symbol_does_not_mute_this_one(h) -> None:
    """键是（用户, 市场, 代码）：把代码漏掉的话，一只没人看的票会静音整个账户。"""
    h.add(filters={"no_open_alert": True})
    h.seed_hit(rule_id="r-elsewhere", code=ANOTHER_CODE)
    assert len(h.engine().check(USER).raised) == 1


# --- 展开：一条规则到底读了谁 ---------------------------------------------------------------


def test_a_watchlist_rule_does_not_expand_into_another_users_list(container) -> None:
    """自选是「这个人的清单」。展开成别人的话，甲的止损会替乙喊，而乙从没订过它。"""
    lists = container.watchlists
    lists.add(USER, MARKET, CODE)
    lists.add(OTHER, MARKET, ANOTHER_CODE)
    rule = container.rules_store.create(
        USER,
        kind="price.cross_below",
        name="甲的自选",
        target_type="watchlist",
        target_ref="*",
        params={"price": 1.0},
    )
    assert sorted(target.code for target in container.rule_data.expand(rule)) == [CODE]


# --- 一趟里的部分失败：一条停，别的一条都不许停 ----------------------------------------------


def test_a_rule_missing_a_required_param_is_skipped_not_a_crash(h) -> None:
    """库里那一行是**上一个版本的模板**写的：老行缺了新必填键，屏幕上不许出现一段栈。

    `KeyError` 不是 `SamsError`，命令层那句 `except SamsError` 兜不住它。这一枚先把键名
    从 `params_json` 里抹掉，再要求它变成一句说得出名字的 skipped。
    """
    first = h.add()
    h.db.execute("UPDATE rules SET params_json = '{}' WHERE id = ?", [first.id])
    h.add(name="乙")
    report = h.engine().check(USER)
    assert len(report.skipped) == 1
    assert "price" in report.skipped[0].reason
    assert len(report.raised) == 1


def test_a_send_failure_is_reported_and_the_run_finishes(h) -> None:
    """投递是引擎够得着的唯一外部动作：它抛了，这一条没有结论，其余照判。"""
    h.add(name="甲")
    h.add(name="乙")
    engine = h.storing(raises=StorageError("库里写不进这一行"))
    report = engine.check(USER)
    assert report.raised == ()
    assert len(report.skipped) == 2
    assert "送不出去" in report.skipped[0].reason
    assert h.alerts.list_for(USER) == []


def test_the_row_a_real_send_wrote_says_what_the_report_said(h) -> None:
    """引擎递给屏幕的那份，与库里那一行必须同源：`sams alert show` 念的是后者。

    两份一旦分叉，`rule check` 屏幕上那句「9.50 CNY 跌破 10.00 CNY」与一小时后 `alert show`
    念出来的就不是同一件事，而复盘要看的正是后者。这里也是 `StoringSender` 存在的理由：
    替身不写库的话，这一条断言根本没有对象可断。
    """
    rule = h.add()
    engine = h.storing()
    raised = engine.check(USER).raised[0]
    row = h.alerts.get(raised.alert_id)
    assert row is not None
    assert (row.rule_id, row.user_id, row.stage, row.severity) == (
        rule.id,
        USER,
        raised.stage,
        raised.severity,
    )
    assert row.payload["value_text"] == raised.value_text
    assert row.payload["threshold_text"] == raised.threshold_text
    assert row.payload["action_note"] == raised.action_note
    assert row.account_id is None
    # 正文不上 `Raised`（那一份只管屏幕上列一条要的那几项），它只进库里那一行：
    # `sams alert show` 念的就是它，所以这里必须与引擎递出去的那一份同源
    (sent,) = engine.sender.sent
    assert row.payload["extra"]["body"] == sent.body
