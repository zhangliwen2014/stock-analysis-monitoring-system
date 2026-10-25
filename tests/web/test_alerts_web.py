"""告警收件箱：筛选、日期窗口、归属 404、已处理/驳回、重发、分页。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sams.core.errors import DatabaseLocked, ValidationError
from sams.core.notify import MID, NO_CHANNEL, SYSTEM_MARKET
from sams.monitor.router import NotifyService
from sams.storage.repos.alerts import DISMISSED, HANDLED, NEW
from sams.web.display import moment_label
from sams.web.pages.alerts import GROUP_SPLIT_NOTE, MARKET_SYSTEM_LABEL


def _seed(ctx, *, user_id, market="A", code="600519", severity=MID, **overrides):
    """落一条告警，绕开页面：这一批测试测的是收件箱怎么读、怎么按，不是怎么发。

    `create` 那几句文字全是必填 keyword（它们进 `payload`，不是列），`triggered_at`
    必须带时区——`_moment` 对裸值直接拒。
    """
    kwargs = dict(
        rule_id=None,
        rule_name="止损线",
        account_id=None,
        account_name="",
        market=market,
        code=code,
        symbol_name="贵州茅台" if code == "600519" else code,
        severity=severity,
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


def test_inbox_lists_and_filters(logged):
    ctx = logged.context
    user = _alice(ctx)
    _seed(ctx, user_id=user.id)
    _seed(ctx, user_id=user.id, market="US", code="AAPL", severity="strong")

    page = logged.client.get("/alerts")
    assert page.status_code == 200
    assert "600519" in page.text and "AAPL" in page.text

    by_market = logged.client.get("/alerts", params={"market": "US"})
    assert "600519" not in by_market.text and "AAPL" in by_market.text

    by_severity = logged.client.get("/alerts", params={"severity": "strong"})
    assert "600519" not in by_severity.text and "AAPL" in by_severity.text


def test_inbox_market_filter_canonicalises_the_query_value(logged):
    """`?market=a` 与 `?market=A` 是同一个筛选：库里那一列只有归一后的键。

    不折一次的话 `a` 筛出空列表，屏幕上说的是「没有命中的告警」而告警明明在——
    表单已经换成下拉，还能交出 `a` 的只剩手改 URL 的人。
    """
    ctx = logged.context
    user = _alice(ctx)
    _seed(ctx, user_id=user.id)
    _seed(ctx, user_id=user.id, market="US", code="AAPL")

    loose = logged.client.get("/alerts", params={"market": "a"})
    assert loose.status_code == 200
    assert "600519" in loose.text and "AAPL" not in loose.text


def test_inbox_rejects_an_unknown_severity_and_names_the_allowed_ones(logged):
    """空列表不是对的答案：一个填错的档位对着一堆存在的告警说「没有命中」是坏页面。"""
    user = _alice(logged.context)
    _seed(logged.context, user_id=user.id)

    response = logged.client.get("/alerts", params={"severity": "urgent"})
    assert response.status_code == 400
    # 名单出自 `SEVERITIES`（页面上只有这句话会一次列全三个，下拉里它们是分开的三个 option）
    assert "只有 strong、mid、weak" in response.text
    assert "urgent" in response.text


def test_inbox_date_window_uses_display_tz_days(logged):
    ctx = logged.context
    user = _alice(ctx)
    _seed(ctx, user_id=user.id, triggered_at=datetime.now(UTC) - timedelta(days=3))
    _seed(ctx, user_id=user.id, code="000001")
    # 「今天」按 display_tz 切：那一列时刻印的就是那个时区，窗口必须与它同一口径
    today = datetime.now(ZoneInfo(ctx.settings.server.display_tz)).date()

    since = logged.client.get("/alerts", params={"since": today.isoformat()})
    assert "000001" in since.text and "600519" not in since.text

    # `until` 含那一天整天（仓库那一边它是开区间，所以界取的是次日零点）
    day = logged.client.get(
        "/alerts", params={"since": today.isoformat(), "until": today.isoformat()}
    )
    assert "000001" in day.text and "600519" not in day.text

    before = logged.client.get("/alerts", params={"until": (today - timedelta(days=1)).isoformat()})
    assert "600519" in before.text and "000001" not in before.text


def test_other_users_alert_is_404(logged):
    ctx = logged.context
    mine = _seed(ctx, user_id=_alice(ctx).id)
    bob = ctx.users.create("bob", "another-correct-horse")
    theirs = _seed(ctx, user_id=bob.id)

    # 自己那条打得开：下面的 404 才是「不是你的」，不是「压根没有这一页」
    assert logged.client.get(f"/alerts/{mine.id}").status_code == 200
    assert logged.client.get(f"/alerts/{theirs.id}").status_code == 404
    acted = logged.client.post(
        f"/alerts/{theirs.id}/acted", data={"csrf": logged.csrf, "note": "x"}
    )
    assert acted.status_code == 404
    # 越权的这一次什么都没改
    assert ctx.alerts_store.get(theirs.id).status == NEW


def test_acted_marks_handled_and_leaves_default_inbox(logged):
    ctx = logged.context
    alert = _seed(ctx, user_id=_alice(ctx).id)

    resp = logged.client.post(
        f"/alerts/{alert.id}/acted",
        data={"csrf": logged.csrf, "action_kind": "other", "note": "已减仓"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/alerts/{alert.id}"
    row = ctx.alerts_store.get(alert.id)
    assert row.status == HANDLED and row.handle_note == "已减仓"

    detail = logged.client.get(f"/alerts/{alert.id}")
    assert "已减仓" in detail.text
    assert "已处理" in detail.text
    # 收件箱默认只看未处理：处理完的那条不再占屏幕
    assert "止损线" not in logged.client.get("/alerts").text
    assert "止损线" in logged.client.get("/alerts", params={"status": "all"}).text


def test_dismiss_requires_code_then_records_it(logged):
    ctx = logged.context
    alert = _seed(ctx, user_id=_alice(ctx).id)

    blank = logged.client.post(f"/alerts/{alert.id}/dismiss", data={"csrf": logged.csrf})
    assert blank.status_code == 400
    # 那句话长在表单旁边（原因码下拉旁边），而不是一屏 error.html
    assert "忽略必须先选原因码" in blank.text
    assert ctx.alerts_store.get(alert.id).status == NEW

    ok = logged.client.post(
        f"/alerts/{alert.id}/dismiss",
        data={"csrf": logged.csrf, "code": "expected", "reason": "假信号：还没碰到止损线"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    row = ctx.alerts_store.get(alert.id)
    assert row.status == DISMISSED and row.dismiss_reason == "假信号：还没碰到止损线"
    assert "已驳回" in logged.client.get(f"/alerts/{alert.id}").text


def test_resend_renders_the_dispatch(logged):
    ctx = logged.context
    alert = _seed(ctx, user_id=_alice(ctx).id)

    resp = logged.client.post(f"/alerts/{alert.id}/resend", data={"csrf": logged.csrf})
    assert resp.status_code == 200
    # 一个通道都没配：选路拒绝并留下 no_channel 那一行，页面上要看得见
    assert "no_channel" in resp.text
    assert "名下没有启用的通道" in resp.text
    # 重发只动投递，不动状态机（`deliver_existing` 不碰 `alerts.status`）
    assert ctx.alerts_store.get(alert.id).status == NEW


def test_resend_keeps_the_page_when_routing_refuses(logged, monkeypatch):
    """按钮是人按的，拒绝就得回到这一屏说：不该是一屏 500，也不该装作成功。"""
    ctx = logged.context
    alert = _seed(ctx, user_id=_alice(ctx).id)

    def refuse(self, alert_id, *, context_provider=None):
        raise ValidationError("规则指定的通道都不可用：主群（已停用）")

    monkeypatch.setattr(NotifyService, "deliver_existing", refuse)
    resp = logged.client.post(f"/alerts/{alert.id}/resend", data={"csrf": logged.csrf})
    assert resp.status_code == 400
    assert "规则指定的通道都不可用：主群（已停用）" in resp.text
    assert ctx.alerts_store.get(alert.id).status == NEW


def test_resend_on_a_locked_database_is_503(logged, monkeypatch):
    """库被调度线程握着不是「发不出去」：那句该由全局处理器答 503，别被兜 SamsError 吞掉。"""
    ctx = logged.context
    alert = _seed(ctx, user_id=_alice(ctx).id)

    def locked(self, alert_id, *, context_provider=None):
        raise DatabaseLocked("sams.duckdb", detail="Conflicting lock (PID 42)")

    monkeypatch.setattr(NotifyService, "deliver_existing", locked)
    resp = logged.client.post(f"/alerts/{alert.id}/resend", data={"csrf": logged.csrf})
    assert resp.status_code == 503


def test_detail_lists_the_delivery_rows(logged):
    ctx = logged.context
    alert = _seed(ctx, user_id=_alice(ctx).id)
    # 直接落一行投递记录：这一条测的是记录上了屏，不是选路本身
    ctx.alerts_store.record(
        alert.id, None, NO_CHANNEL, error="规则指定的通道都不可用：主群（已停用）", attempts=2
    )

    page = logged.client.get(f"/alerts/{alert.id}")
    assert page.status_code == 200
    assert "规则指定的通道都不可用：主群（已停用）" in page.text
    assert "no_channel" in page.text
    # 时刻折到 display_tz 才上屏：同一条告警的几次尝试只差几秒，那一列得到秒
    row = ctx.alerts_store.deliveries(alert.id)[0]
    label = moment_label(row.ts, ctx.settings.server.display_tz, seconds=True)
    assert label in page.text


def test_inbox_pages(logged):
    ctx = logged.context
    user = _alice(ctx)
    # 每条一个 distinct 时刻：`list_for` 按 `triggered_at DESC, id DESC` 排，同一秒里
    # 谁去哪一页由随机 uuid 决定，那样写的测试会时好时坏
    now = datetime.now(UTC)
    for index in range(51):
        _seed(
            ctx,
            user_id=user.id,
            code=f"6{index:05d}",
            triggered_at=now - timedelta(minutes=50 - index),
        )

    first = logged.client.get("/alerts")
    second = logged.client.get("/alerts", params={"page": 2})
    assert "600050" in first.text and "600000" not in first.text
    assert "下一页" in first.text
    assert "600000" in second.text and "下一页" not in second.text
    # 翻页链接带着当前筛选，否则第二页悄悄换成另一份结果：`market=A` 这一串只可能来自链接
    # （下拉里那个键写的是 `name="market"` 与 `value="A"`）
    with_filter = logged.client.get("/alerts", params={"page": 2, "market": "A"})
    assert with_filter.status_code == 200
    assert "market=A" in with_filter.text


def test_inbox_can_filter_to_system_alerts_and_the_select_shows_it(logged):
    """`SYS` 必须是下拉里的一档：系统告警与真告警同表，只靠 `market = 'SYS'` 分开
    （`AlertRepo.has_system_alert`），筛不出来等于这一类告警在收件箱里没有入口；
    而手改 `?market=SYS` 时选中的那个 option 必须真在名单里，否则页面一边说
    「全部市场」一边只列 SYS。"""
    ctx = logged.context
    user = _alice(ctx)
    _seed(ctx, user_id=user.id)
    _seed(ctx, user_id=user.id, market=SYSTEM_MARKET, code="scheduler", rule_name=None)

    page = logged.client.get("/alerts", params={"market": SYSTEM_MARKET})
    assert page.status_code == 200
    assert "scheduler" in page.text and "600519" not in page.text
    assert f'<option value="{SYSTEM_MARKET}" selected>{MARKET_SYSTEM_LABEL}</option>' in page.text
    # 不筛时它也得在名单里躺着（否则这一档只有手改 URL 的人能用）
    assert f'<option value="{SYSTEM_MARKET}">' in logged.client.get("/alerts").text


def test_inbox_rejects_an_unknown_market_and_names_the_allowed_ones(logged):
    """填错的市场要答 400，跟档位同一口径：`?market=xyz` 折成 `XYZ` 后筛出空列表，
    那一屏会对着一堆确实存在的告警说「没有命中的告警」——那句话是假的。"""
    ctx = logged.context
    _seed(ctx, user_id=_alice(ctx).id)

    response = logged.client.get("/alerts", params={"market": "xyz"})
    assert response.status_code == 400
    allowed = "、".join([*sorted(ctx.settings.markets), SYSTEM_MARKET])
    assert f"只有 {allowed}" in response.text
    assert "XYZ" in response.text


def test_inbox_rejects_a_page_that_is_not_a_number(logged):
    """页码不是数字是敲错了，不是偏好：静默折回第 1 页会让人以为是筛选变了，
    而这一页的其它输入都是这么答的。"""
    response = logged.client.get("/alerts", params={"page": "two"})
    assert response.status_code == 400
    assert "页码要的是整数" in response.text and "two" in response.text


def test_inbox_rejects_a_page_below_one(logged):
    response = logged.client.get("/alerts", params={"page": "0"})
    assert response.status_code == 400
    assert "页码从 1 开始" in response.text


def test_inbox_a_page_past_the_end_says_so_and_keeps_the_prev_link(logged):
    """越界的那一页不能说「没有命中的告警」：51 条确实命中，只是不在这个页码上。"""
    ctx = logged.context
    user = _alice(ctx)
    now = datetime.now(UTC)
    for index in range(51):
        _seed(
            ctx,
            user_id=user.id,
            code=f"6{index:05d}",
            triggered_at=now - timedelta(minutes=50 - index),
        )

    past = logged.client.get("/alerts", params={"page": 999})
    assert past.status_code == 200
    assert "没有命中的告警" not in past.text
    assert "这一页没有内容" in past.text
    # 上一页还得能点回去，否则人只能改 URL
    assert "上一页" in past.text


def test_inbox_says_a_group_can_be_split_only_when_the_filter_is_paginated(logged):
    """分组是按页各算一次的：同一标的的告警可能在两页里各出现一次标题，
    这句话只在真的分页时说出来，不分页的那一屏不该有噪音。"""
    ctx = logged.context
    user = _alice(ctx)
    _seed(ctx, user_id=user.id, code="000001", triggered_at=datetime.now(UTC))
    single = logged.client.get("/alerts")
    assert single.status_code == 200
    assert GROUP_SPLIT_NOTE not in single.text

    now = datetime.now(UTC)
    for index in range(51):
        _seed(
            ctx,
            user_id=user.id,
            code=f"6{index:05d}",
            triggered_at=now - timedelta(minutes=50 - index),
        )

    first = logged.client.get("/alerts")
    assert first.status_code == 200
    assert GROUP_SPLIT_NOTE in first.text


def test_inbox_marks_a_test_alert_in_the_row(logged):
    """`monitor/router.py` 那句「收件箱里带『测试』徽标」说的是列表：
    只有详情页有徽标的话，/settings/test-alert 那条（D131）在列表里和真告警长得一样。"""
    ctx = logged.context
    user = _alice(ctx)
    _seed(ctx, user_id=user.id, is_test=True)
    _seed(ctx, user_id=user.id, code="000001")

    page = logged.client.get("/alerts")
    assert page.status_code == 200
    # 恰好一个：测试那一条有、真告警那一条没有
    assert page.text.count(">测试<") == 1


def test_detail_says_a_resend_can_double_send(logged):
    """重发不走队列、不抢租约、也不去重（`deliver_existing`），而队列那一侧只有
    `qsize`/`empty`，认不出「这一条还在不在队里」：这句话只能写在按钮旁边。"""
    ctx = logged.context
    alert = _seed(ctx, user_id=_alice(ctx).id)

    page = logged.client.get(f"/alerts/{alert.id}")
    assert page.status_code == 200
    assert "再发一次" in page.text
    assert "会被再发一次" in page.text


def test_dismiss_400_refills_what_was_typed(logged):
    """被拒的重渲染要把人刚敲的那份交回框里（同 `pages/rules.py`）：清掉它等于让人
    从零再敲一遍。今天真会拒的那一句是空原因，所以交的是「敲了空格、被 strip 成空」——
    它同时钉住交回的是**原文**而不是折过的那份（折过的话这里会是空串）。"""
    ctx = logged.context
    alert = _seed(ctx, user_id=_alice(ctx).id)

    resp = logged.client.post(
        f"/alerts/{alert.id}/dismiss",
        data={"csrf": logged.csrf, "code": "other", "reason": "   "},
    )
    assert resp.status_code == 400
    assert "other 必须写一句说明" in resp.text
    assert 'value="   "' in resp.text
