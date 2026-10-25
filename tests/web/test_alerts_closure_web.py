"""闭环命令与详情（M4-B §6.3/§8.1）：ack/defer/undo、五动作、忽略码、事件历史、同步批注。

seed 走仓库不走页面：这一批测的是闭环那一屏怎么按，不是告警怎么发出来。
`_seed` 与 `test_alerts_web.py` 同款——不建 tests/web/support.py（那会遮住根目录的
`support`，见该文件模块注释），重复的是二十行装配，不是逻辑。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sams.core.notify import MID
from sams.storage.repos.alerts import ACKNOWLEDGED, DISMISSED, HANDLED, NEW


def _seed(ctx, *, user_id, market="A", code="600519", **overrides):
    kwargs = dict(
        rule_id=None,
        rule_name="止损线",
        account_id=None,
        account_name="",
        market=market,
        code=code,
        symbol_name="贵州茅台",
        severity=MID,
        stage="close",
        value_text="1500.00",
        threshold_text="1550.00",
        action_note="考虑减仓",
        triggered_at=datetime.now(UTC),
    )
    kwargs.update(overrides)
    return ctx.alerts_store.create(user_id, **kwargs)


def _alice(ctx):
    return ctx.users.get_by_username("alice")


def _acted(logged, alert_id, **overrides):
    data = {"csrf": logged.csrf, "action_kind": "other", "note": "按提示减半仓"}
    data.update(overrides)
    return logged.client.post(f"/alerts/{alert_id}/acted", data=data, follow_redirects=False)


def test_ack_marks_read_idempotent_and_lands_in_events(logged):
    ctx = logged.context
    alert = _seed(ctx, user_id=_alice(ctx).id)
    for _ in range(2):  # 手机连点两下不该多出一条历史
        resp = logged.client.post(
            f"/alerts/{alert.id}/ack", data={"csrf": logged.csrf}, follow_redirects=False
        )
        assert resp.status_code == 303
    row = ctx.alerts_store.get(alert.id)
    assert row.status == ACKNOWLEDGED and row.acknowledged_at is not None
    events = ctx.alerts_store.events.list_for(alert.id)
    assert [e.action for e in events] == ["ack"]
    detail = logged.client.get(f"/alerts/{alert.id}")
    assert "动作历史" in detail.text and "已读" in detail.text
    # 「已读」按钮只在 new 上出现；处置/忽略在 acknowledged 上仍是按钮
    assert f"/alerts/{alert.id}/ack" not in detail.text
    assert "处置" in detail.text


def test_ack_after_terminal_is_409_and_says_which_door(logged):
    ctx = logged.context
    alert = _seed(ctx, user_id=_alice(ctx).id)
    ctx.alerts_store.act(alert.id, user_id=_alice(ctx).id, action_kind="other", note="先关上")
    resp = logged.client.post(f"/alerts/{alert.id}/ack", data={"csrf": logged.csrf})
    assert resp.status_code == 409
    assert "不能再标已读" in resp.text  # 仓库那句话原样上屏：哪扇门走不通说得清


def test_defer_choices_write_future_moments_without_touching_status(logged):
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)

    resp = logged.client.post(
        f"/alerts/{alert.id}/defer", data={"csrf": logged.csrf, "until": "1h"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = ctx.alerts_store.get(alert.id)
    assert row.status == NEW  # 稍后不是终态（D138）
    now = datetime.now(UTC)
    assert now + timedelta(minutes=55) <= row.deferred_until <= now + timedelta(minutes=65)

    resp = logged.client.post(
        f"/alerts/{alert.id}/defer", data={"csrf": logged.csrf, "until": "open"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = ctx.alerts_store.get(alert.id)
    sh = row.deferred_until.astimezone(ZoneInfo("Asia/Shanghai"))
    assert (sh.hour, sh.minute) == (9, 30)  # StubCalendar：工作日 09:30 开盘
    assert sh.weekday() < 5
    detail = logged.client.get(f"/alerts/{alert.id}")
    assert "稍后至" in detail.text  # 「你让它稍后提醒」那一格

    bad = logged.client.post(
        f"/alerts/{alert.id}/defer", data={"csrf": logged.csrf, "until": "5m"}
    )
    assert bad.status_code == 400
    assert "明天开盘" in bad.text  # 拒绝的话里带着那三个合法档


def test_acted_requires_a_known_action_kind(logged):
    ctx = logged.context
    alert = _seed(ctx, user_id=_alice(ctx).id)
    blank = logged.client.post(f"/alerts/{alert.id}/acted", data={"csrf": logged.csrf})
    assert blank.status_code == 400
    assert "未知动作类型" in blank.text
    assert ctx.alerts_store.get(alert.id).status == NEW
    zz = _acted(logged, alert.id, action_kind="zzz")
    assert zz.status_code == 400
    # 名单上屏时说的是人话不是键名（`ACTION_LABELS` 与 `ACTION_KINDS` 一一对应）
    assert "成交" in zz.text and "加入自选" in zz.text


def test_acted_other_requires_note(logged):
    ctx = logged.context
    alert = _seed(ctx, user_id=_alice(ctx).id)
    resp = _acted(logged, alert.id, note="   ")
    assert resp.status_code == 400
    assert "other 必须写一句说明" in resp.text
    assert ctx.alerts_store.get(alert.id).status == NEW


def test_acted_watch_adds_to_watchlist_atomically(logged):
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)
    resp = _acted(logged, alert.id, action_kind="watch", note="")
    assert resp.status_code == 303
    row = ctx.alerts_store.get(alert.id)
    assert row.status == HANDLED and row.action_kind == "watch"
    assert ctx.watchlists.contains(alice.id, "A", "600519")


def test_acted_thesis_must_be_yours_and_about_the_same_symbol(logged):
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)
    missing = _acted(logged, alert.id, action_kind="thesis", thesis_ref="no-such")
    assert missing.status_code == 400 and "不存在" in missing.text

    other = ctx.theses_store.create(
        user_id=alice.id, market="A", code="000001", style="value",
        actor_id=alice.id, now=datetime.now(UTC),
    )
    crossed = _acted(logged, alert.id, action_kind="thesis", thesis_ref=other.id)
    assert crossed.status_code == 400 and "不符" in crossed.text

    mine = ctx.theses_store.create(
        user_id=alice.id, market="A", code="600519", style="value",
        actor_id=alice.id, now=datetime.now(UTC),
    )
    ok = _acted(logged, alert.id, action_kind="thesis", thesis_ref=mine.id)
    assert ok.status_code == 303
    row = ctx.alerts_store.get(alert.id)
    assert row.action_kind == "thesis" and row.action_ref == mine.id
    detail = logged.client.get(f"/alerts/{alert.id}")
    assert f"/theses/{mine.id}" in detail.text  # 关联对象可点（§6.3 终态区）


def test_acted_rule_links_its_edit_page(logged):
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)
    rule = ctx.rules_store.create(
        alice.id, kind="stop.fixed", name="茅台止损",
        target_type="symbol", target_ref="A/600519", params={"price": 1400.0},
    )
    resp = _acted(logged, alert.id, action_kind="rule", rule_ref=rule.id)
    assert resp.status_code == 303
    assert ctx.alerts_store.get(alert.id).action_ref == rule.id
    assert f"/rules/{rule.id}/edit" in logged.client.get(f"/alerts/{alert.id}").text


def test_acted_trade_fills_and_closes_in_one_go(logged):
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)
    account = ctx.ledger.open_account("alice", "测试户")
    # 存款要盖得住这笔成交（100×1500+费 5 = 150,005）：无杠杆账户不透支（§5.4），
    # 钱不够时先被账本拒的就是下面要测的 303
    ctx.ledger.deposit(
        account.id, "CNY", 200_000.0, username="alice", reason="初始", actor_id=alice.id
    )
    resp = _acted(
        logged, alert.id,
        action_kind="trade", account_id=account.id, side="buy",
        qty="100", price="1500", fee="5", note="",
    )
    assert resp.status_code == 303
    row = ctx.alerts_store.get(alert.id)
    assert row.status == HANDLED and row.action_kind == "trade"
    assert ctx.db.scalar(
        "SELECT count(*) FROM sim_orders WHERE account_id = ? AND code = ?",
        [account.id, "600519"],
    ) == 1
    # 成交单号在终态区可读（没有 order 详情页，留明文，见计划头「明确不做」）
    assert row.action_ref in logged.client.get(f"/alerts/{alert.id}").text


def test_acted_trade_bad_number_writes_neither_order_nor_closure(logged):
    """一半成一半败不允许存在：数量读不出数字时，闭环与成交都不该落。"""
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)
    account = ctx.ledger.open_account("alice", "测试户")
    resp = _acted(
        logged, alert.id,
        action_kind="trade", account_id=account.id, side="buy",
        qty="abc", price="1500", note="",
    )
    assert resp.status_code == 400
    assert "数量 要是数字" in resp.text
    assert ctx.alerts_store.get(alert.id).status == NEW
    assert ctx.db.scalar(
        "SELECT count(*) FROM sim_orders WHERE account_id = ?", [account.id]
    ) == 0


def test_sync_annotation_checkbox_writes_a_prefilled_symbol_note(logged):
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)
    resp = _acted(logged, alert.id, sync_annotation="on")
    assert resp.status_code == 303
    made = ctx.annotations.list_target(alice.id, "symbol", "A:600519")[0]
    assert "闭环" in made.body_md
    assert "按提示减半仓" in made.body_md  # 处置结论进了正文（D137）
    assert alert.id in made.body_md  # 带回链


def test_dismiss_requires_code_then_records_it_with_words(logged):
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)

    blank = logged.client.post(f"/alerts/{alert.id}/dismiss", data={"csrf": logged.csrf})
    assert blank.status_code == 400
    assert "忽略必须先选原因码" in blank.text
    assert "误报" in blank.text  # 名单上屏用人话

    thin = logged.client.post(
        f"/alerts/{alert.id}/dismiss", data={"csrf": logged.csrf, "code": "misreport"}
    )
    assert thin.status_code == 400
    assert "misreport 必须写一句说明" in thin.text

    ok = logged.client.post(
        f"/alerts/{alert.id}/dismiss",
        data={"csrf": logged.csrf, "code": "expected", "sync_annotation": "on"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    row = ctx.alerts_store.get(alert.id)
    assert row.status == DISMISSED and row.dismiss_code == "expected"
    # 「符合预期」不强制说明；勾选同步批注照样写
    made = ctx.annotations.list_target(alice.id, "symbol", "A:600519")[0]
    assert "忽略（符合预期）" in made.body_md
    assert "这条原因会进入本周忽略率" in logged.client.get(f"/alerts/{alert.id}").text


def test_undo_restores_previous_state(logged):
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)
    _acted(logged, alert.id)
    resp = logged.client.post(
        f"/alerts/{alert.id}/undo", data={"csrf": logged.csrf}, follow_redirects=False
    )
    assert resp.status_code == 303
    row = ctx.alerts_store.get(alert.id)
    assert row.status == NEW  # 上一态是 new（events.last 的 from_status）
    assert row.handled_at is None and row.action_kind is None
    assert [e.action for e in ctx.alerts_store.events.list_for(alert.id)] == ["act", "undo"]


def test_undo_after_window_hides_the_button(logged):
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)
    _acted(logged, alert.id)
    # 把终态时刻拨回 6 分钟前：按钮随窗口消失，而不是按下去吃 409
    ctx.db.execute(
        "UPDATE alerts SET handled_at = ? WHERE id = ?",
        [datetime.now(UTC) - timedelta(minutes=6), alert.id],
    )
    detail = logged.client.get(f"/alerts/{alert.id}")
    assert f"/alerts/{alert.id}/undo" not in detail.text
    forced = logged.client.post(f"/alerts/{alert.id}/undo", data={"csrf": logged.csrf})
    assert forced.status_code == 409
    assert "撤销窗口已过" in forced.text
    assert ctx.alerts_store.get(alert.id).status == HANDLED


def test_detail_embeds_the_annotation_panel_and_thesis_picker(logged):
    """上下文区与推送同源：详情嵌的就是那枚批注面板 fragment（§6.3）。"""
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)
    ctx.annotations.create(
        alice.id, target_type="symbol", target_ref="A:600519", body_md="**渠道**库存见底"
    )
    ctx.theses_store.create(
        user_id=alice.id, market="A", code="600519", style="value",
        actor_id=alice.id, now=datetime.now(UTC),
    )
    detail = logged.client.get(f"/alerts/{alert.id}")
    assert "target_type=symbol" in detail.text and "A:600519" in detail.text
    # 引用选择器把本人的同标的观点列进下拉
    assert "关联的观点" in detail.text and "value" in detail.text
