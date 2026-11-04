"""S1-B 的网页那一头：触发 K 线、单票静音、批量已读。

seed 走仓库不走页面：这一批测的是告警页怎么读、怎么按，不是规则怎么判出来。
`_seed` 与 `test_alerts_web.py` 同款。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sams.core.notify import MID, SYSTEM_MARKET
from sams.storage.repos.alerts import ACKNOWLEDGED


def _seed(ctx, *, user_id, market="A", code="600519", **overrides):
    kwargs = dict(
        rule_id=None,
        rule_name="止损线",
        account_id=None,
        account_name="",
        market=market,
        code=code,
        symbol_name="贵州茅台" if code == "600519" else code,
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


def test_detail_links_to_the_bar_that_triggered_it(logged):
    """证据链：详情页要指得回判出这条告警的那根 bar，且回得来。"""
    ctx = logged.context
    alert = _seed(ctx, user_id=_alice(ctx).id, trigger_bar_ts=date(2026, 10, 30))

    text = logged.client.get(f"/alerts/{alert.id}").text
    assert "触发 K 线" in text
    assert "2026-10-30" in text
    assert "/symbol/A/600519?focus=2026-10-30" in text
    # 返回地址是这一条详情：从图上退回来仍是同一条告警，不是一张光秃秃的个股页
    assert f"back=%2Falerts%2F{alert.id}" in text


def test_a_history_row_without_the_bar_says_dash_and_guesses_nothing(logged):
    alert = _seed(logged.context, user_id=_alice(logged.context).id)

    text = logged.client.get(f"/alerts/{alert.id}").text
    assert "触发 K 线" in text
    assert "focus=" not in text


def test_mute_the_symbol_then_detail_and_inbox_say_so(logged):
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)

    resp = logged.client.post(
        f"/alerts/{alert.id}/mute",
        data={"csrf": logged.csrf, "note": "财报前别吵"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    mute = ctx.alert_mutes.get(alice.id, "A", "600519")
    assert mute is not None and mute.note == "财报前别吵"

    detail = logged.client.get(f"/alerts/{alert.id}").text
    assert "取消静音" in detail
    assert "财报前别吵" in detail
    assert f"/alerts/{alert.id}/unmute" in detail
    # 静音的单位是票，不是这一条告警：页面上要写明
    assert "这只票的全部规则" in detail

    inbox = logged.client.get("/alerts").text
    assert "已静音" in inbox

    resp = logged.client.post(
        f"/alerts/{alert.id}/unmute", data={"csrf": logged.csrf}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert ctx.alert_mutes.get(alice.id, "A", "600519") is None
    assert "已静音" not in logged.client.get("/alerts").text


def test_a_system_alert_has_no_mute_button(logged):
    """系统告警不走引擎那一趟，静音键对它没有任何读者：不摆一个假的按钮。"""
    alert = _seed(
        logged.context,
        user_id=_alice(logged.context).id,
        market=SYSTEM_MARKET,
        code="test",
    )
    text = logged.client.get(f"/alerts/{alert.id}").text
    assert "单票静音" not in text
    assert f"/alerts/{alert.id}/mute" not in text


def test_muting_is_scoped_to_the_user_who_pressed_it(logged):
    """静音键带 user_id：同一条规则换个人不该继承上一个人的静音。"""
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)
    bob = ctx.users.create("bob", "Bob-1234")

    logged.client.post(
        f"/alerts/{alert.id}/mute", data={"csrf": logged.csrf}, follow_redirects=False
    )
    assert ctx.alert_mutes.get(alice.id, "A", "600519") is not None
    assert ctx.alert_mutes.get(bob.id, "A", "600519") is None


def test_batch_ack_marks_only_new_rows_and_skips_the_rest(logged):
    ctx = logged.context
    alice = _alice(ctx)
    first = _seed(ctx, user_id=alice.id)
    second = _seed(ctx, user_id=alice.id, code="000001")
    done = _seed(ctx, user_id=alice.id, code="000002")
    ctx.alerts_store.acknowledge(done.id, user_id=alice.id)

    resp = logged.client.post(
        "/alerts/ack",
        data={
            "csrf": logged.csrf,
            "ids": [first.id, second.id, done.id],
            "next": "/alerts?status=all",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/alerts?status=all&acked=2&skipped=1"
    assert ctx.alerts_store.get(first.id).status == ACKNOWLEDGED
    assert ctx.alerts_store.get(second.id).status == ACKNOWLEDGED
    # 每一条都照旧落一条事件：批量不是一条「批量」事件
    assert [e.action for e in ctx.alerts_store.events.list_for(first.id)] == ["ack"]

    page = logged.client.get(resp.headers["location"]).text
    assert "已标已读 2 条，跳过 1 条" in page


def test_a_duplicate_id_in_the_batch_is_not_counted_as_skipped(logged):
    """连点两下/同一个值交两次：第一条标了，第二条只是「已经标过」，不是被拒。"""
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)

    resp = logged.client.post(
        "/alerts/ack",
        data={"csrf": logged.csrf, "ids": [alert.id, alert.id]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/alerts?acked=1&skipped=0"


def test_an_offsite_next_is_dropped_instead_of_becoming_a_redirect_target(logged):
    ctx = logged.context
    alice = _alice(ctx)

    for target in ("https://evil.example/alerts", "//evil.example/alerts", "/rules"):
        alert = _seed(ctx, user_id=alice.id)
        resp = logged.client.post(
            "/alerts/ack",
            data={"csrf": logged.csrf, "ids": [alert.id], "next": target},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/alerts?")


def test_only_new_rows_carry_a_checkbox(logged):
    ctx = logged.context
    alice = _alice(ctx)
    new = _seed(ctx, user_id=alice.id)
    done = _seed(ctx, user_id=alice.id, code="000001")
    ctx.alerts_store.acknowledge(done.id, user_id=alice.id)

    text = logged.client.get("/alerts?status=all").text
    assert f'name="ids" value="{new.id}"' in text
    assert f'name="ids" value="{done.id}"' not in text
    # 全选勾的是「这一页还挂着的」，不是终态行
    assert "data-ack-all" in text


def test_mute_one_kind_and_unmute_only_that_kind(logged):
    """静音分组（D249）：关掉资讯，价格规则仍该照常。"""
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)

    resp = logged.client.post(
        f"/alerts/{alert.id}/mute",
        data={"csrf": logged.csrf, "note": "资讯别推", "kind": "news"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert ctx.alert_mutes.get(alice.id, "A", "600519", kind="news") is not None
    assert ctx.alert_mutes.get(alice.id, "A", "600519", kind="rule") is None

    detail = logged.client.get(f"/alerts/{alert.id}").text
    assert "资讯别推" in detail
    assert "解除资讯静音" in detail

    resp = logged.client.post(
        f"/alerts/{alert.id}/unmute",
        data={"csrf": logged.csrf, "kind": "news"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert ctx.alert_mutes.get(alice.id, "A", "600519", kind="news") is None


def test_detail_lists_each_muted_kind_with_its_own_button(logged):
    ctx = logged.context
    alice = _alice(ctx)
    alert = _seed(ctx, user_id=alice.id)
    ctx.alert_mutes.add(alice.id, "A", "600519", kind="news", note="只要原文")
    ctx.alert_mutes.add(alice.id, "A", "600519", kind="digest")

    detail = logged.client.get(f"/alerts/{alert.id}").text

    assert 'name="kind" value="news"' in detail
    assert 'name="kind" value="digest"' in detail
    assert "解除资讯静音" in detail
    assert "解除摘要静音" in detail
