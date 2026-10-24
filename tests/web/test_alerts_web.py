"""告警收件箱：筛选、日期窗口、归属 404、已处理/驳回、重发、分页。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sams.core.errors import DatabaseLocked, ValidationError
from sams.core.notify import MID, NO_CHANNEL
from sams.monitor.router import NotifyService
from sams.storage.repos.alerts import DISMISSED, HANDLED, NEW
from sams.web.display import moment_label


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
        data={"csrf": logged.csrf, "note": "已减仓"},
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


def test_dismiss_requires_reason_then_records_it(logged):
    ctx = logged.context
    alert = _seed(ctx, user_id=_alice(ctx).id)

    blank = logged.client.post(f"/alerts/{alert.id}/dismiss", data={"csrf": logged.csrf})
    assert blank.status_code == 400
    # 那句话长在表单旁边（驳回原因这一格旁边），而不是一屏 error.html
    assert "驳回必须写原因" in blank.text
    assert ctx.alerts_store.get(alert.id).status == NEW

    ok = logged.client.post(
        f"/alerts/{alert.id}/dismiss",
        data={"csrf": logged.csrf, "reason": "假信号：还没碰到止损线"},
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
