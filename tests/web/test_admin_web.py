"""管理页：非 admin 挡在 403，按钮真的落到仓库、请求队列与运行时。

这一层被钉住的三件事：屏幕上既没有 UUID 也没有 ISO 原文（人名按显示名退回登录名，
时刻折到 display_tz 才上屏）；「带的是要变成什么」那种隐藏 input 读的是值而不是
键在不在（`forms.checked()` 只看键，隐藏 input 永远在，于是降权按下不变）；
一切仓库调用在端点体内走 `web.contexts.current()`（D113）。
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from sams.storage.repos.data_sources import SourceRow
from sams.web.display import moment_label


def _login(client, username: str, password: str) -> str:
    """在**夹具那条 client** 上换人：在外面新建 TestClient 就落在 `with` 之外，
    用例结束没人关它，而 app 的 lifespan（那些线程上下文）也不会走。"""
    response = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return client.cookies.get("sams_csrf").rpartition(".")[0]


def _make_bob(ctx):
    return ctx.users.create("bob", "another-correct-horse")


def _post(client, path: str, csrf: str, **data):
    return client.post(path, data={"csrf": csrf, **data}, follow_redirects=False)


def test_admin_home_points_at_the_users_page(logged):
    home = logged.client.get("/admin", follow_redirects=False)
    assert home.status_code == 303
    assert home.headers["location"] == "/admin/users"


def test_admin_pages_refuse_non_admin(web_app):
    ctx = web_app.context
    _make_bob(ctx)
    csrf = _login(web_app.client, "bob", "another-correct-horse")

    for path in ("/admin", "/admin/users", "/admin/sources", "/admin/channels", "/admin/health"):
        assert web_app.client.get(path).status_code == 403, path
    # 带着真 CSRF 按下去也是 403：挡人的是角色那道闸，不是 CSRF 那道
    denied = _post(web_app.client, "/admin/prune", csrf, confirm="yes")
    assert denied.status_code == 403


def test_users_page_create_duplicate_and_self_guard(logged):
    ctx = logged.context
    alice = ctx.users.get_by_username("alice")

    page = logged.client.get("/admin/users")
    assert page.status_code == 200 and "alice" in page.text

    create = _post(
        logged.client,
        "/admin/users/create",
        logged.csrf,
        username="carol",
        password="a-long-enough-passphrase",
    )
    assert create.status_code == 303
    carol = ctx.users.get_by_username("carol")
    assert carol is not None and not carol.is_admin

    dup = _post(
        logged.client,
        "/admin/users/create",
        logged.csrf,
        username="carol",
        password="a-long-enough-passphrase",
    )
    assert dup.status_code == 400
    assert "用户名已存在" in dup.text, "被拒时念的就是仓库那一句：两处措辞迟早分叉"

    guard = _post(logged.client, f"/admin/users/{alice.id}/disable", logged.csrf)
    assert guard.status_code == 400
    assert ctx.users.get_by_username("alice").status == "active"


def test_the_create_form_checkbox_really_mints_an_admin(logged):
    """复选框用 `checked()` 是对的（勾了键才在），这里钉住它没被顺手改成读值。"""
    ctx = logged.context
    create = _post(
        logged.client,
        "/admin/users/create",
        logged.csrf,
        username="dave",
        password="a-long-enough-passphrase",
        is_admin="1",
    )
    assert create.status_code == 303
    assert ctx.users.get_by_username("dave").is_admin is True


def test_disable_then_enable_a_target_account(logged):
    ctx = logged.context
    bob = _make_bob(ctx)
    ctx.sessions.issue(bob.id, ttl_days=1)

    assert _post(logged.client, f"/admin/users/{bob.id}/disable", logged.csrf).status_code == 303
    assert ctx.users.get(bob.id).status == "disabled"
    assert ctx.sessions.active_count(bob.id) == 0, "停用要连会话一起吊销"

    assert _post(logged.client, f"/admin/users/{bob.id}/enable", logged.csrf).status_code == 303
    assert ctx.users.get(bob.id).status == "active"


def test_an_unknown_target_answers_404(logged):
    assert _post(logged.client, "/admin/users/nobody/disable", logged.csrf).status_code == 404


def test_the_admin_flag_actually_comes_off(logged):
    """降权走的是隐藏 input 的值：`checked()` 只看键在不在，键永远在。

    少了这一条，「降为普通」按下去库里那位还是 admin，而屏幕上那句状态刚刚自己
    翻回了「admin」——没人会去查一个显示成功的操作。
    """
    ctx = logged.context
    bob = _make_bob(ctx)

    assert _post(
        logged.client, f"/admin/users/{bob.id}/set_admin", logged.csrf, is_admin="1"
    ).status_code == 303
    assert ctx.users.get(bob.id).is_admin is True

    assert _post(
        logged.client, f"/admin/users/{bob.id}/set_admin", logged.csrf, is_admin="0"
    ).status_code == 303
    assert ctx.users.get(bob.id).is_admin is False


def test_demoting_yourself_is_refused(logged):
    ctx = logged.context
    alice = ctx.users.get_by_username("alice")
    resp = _post(logged.client, f"/admin/users/{alice.id}/set_admin", logged.csrf, is_admin="0")
    assert resp.status_code == 400
    assert ctx.users.get(alice.id).is_admin is True, "被拒的一次写入一个字都不该留下"


def test_reset_password_kills_target_sessions(logged):
    ctx = logged.context
    bob = _make_bob(ctx)
    ctx.sessions.issue(bob.id, ttl_days=1)
    assert ctx.sessions.active_count(bob.id) == 1

    resp = _post(
        logged.client,
        f"/admin/users/{bob.id}/reset_password",
        logged.csrf,
        password="reset-to-this-one-ok",
    )
    assert resp.status_code == 303
    assert ctx.users.authenticate("bob", "reset-to-this-one-ok") is not None
    assert ctx.sessions.active_count(bob.id) == 0, "重置密码后那个人所有设备都要重新登录"


def test_a_too_short_new_password_stays_on_the_page(logged):
    ctx = logged.context
    bob = _make_bob(ctx)
    resp = _post(
        logged.client, f"/admin/users/{bob.id}/reset_password", logged.csrf, password="short"
    )
    assert resp.status_code == 400
    assert "12" in resp.text
    assert ctx.users.authenticate("bob", "short") is None


def test_users_page_names_people_by_display_name(logged):
    """C8：「显示名」那一列是折好的名字——有显示名用显示名，没有就退回登录名。

    钉的是那一格，不是整页文本：id 合理地出现在每一条操作的 action 里，
    拿「id 不在页面上」当断言永远成立不了（同 `test_dashboard` 里那枚行链接的教训）。
    """
    ctx = logged.context
    bob = _make_bob(ctx)
    ctx.users.set_display_name(bob.id, "老张")
    ctx.users.create("carol", "a-long-enough-passphrase")

    page = logged.client.get("/admin/users")
    named = re.search(r"<td>bob</td>\s*<td>([^<]*)</td>", page.text)
    assert named and named.group(1) == "老张"
    plain = re.search(r"<td>carol</td>\s*<td>([^<]*)</td>", page.text)
    assert plain and plain.group(1) == "carol", "空显示名退回登录名，不是「-」也不是 id"


def test_sync_queues_a_request_for_a_configured_market(logged):
    resp = _post(logged.client, "/admin/sync", logged.csrf, market="A")
    assert resp.status_code == 303 and "sync=queued" in resp.headers["location"]
    drained = logged.core.requests.drain()
    assert len(drained) == 1 and drained[0].market == "A"

    bad = _post(logged.client, "/admin/sync", logged.csrf, market="JP")
    assert bad.status_code == 400
    assert logged.core.requests.drain() == ()
    assert "JP" in bad.text


def test_the_sync_form_lists_the_configured_markets(logged):
    """那个 select 列的是 `settings.markets`，不是写死的 A/HK/US（每一页都这么排）。"""
    page = logged.client.get("/admin/health")
    assert sorted(re.findall(r'<option value="([^"]+)"', page.text)) == sorted(
        logged.context.settings.markets
    )


def test_prune_needs_confirm_and_then_runs(logged):
    first = _post(logged.client, "/admin/prune", logged.csrf)
    assert first.status_code == 303 and "prune=confirm" in first.headers["location"]
    assert logged.runtime.prune_calls == 0, "没确认就不许动手"

    second = _post(logged.client, "/admin/prune", logged.csrf, confirm="yes")
    assert second.status_code == 303 and "prune=accepted" in second.headers["location"]
    assert logged.runtime.prune_calls == 1


def test_a_second_prune_is_refused_and_audited_as_a_denial(logged, caplog):
    """已经在跑时那句「不排队」要落到 redirect 与审计那一行，不能只是按钮没反应。"""
    logged.runtime.request_prune = lambda: False
    with caplog.at_level(logging.INFO, logger="sams.web"):
        resp = _post(logged.client, "/admin/prune", logged.csrf, confirm="yes")
    assert resp.status_code == 303 and "prune=busy" in resp.headers["location"]
    assert logged.runtime.prune_calls == 0
    assert "deny admin.prune" in caplog.text


def test_the_prune_summary_and_its_moment_come_from_the_runtime(logged):
    """时刻在 Python 里折（`moment_label`）：模板里 `.astimezone().strftime()` 会印成 UTC。"""
    _post(logged.client, "/admin/prune", logged.csrf, confirm="yes")
    page = logged.client.get("/admin/health")
    assert page.status_code == 200
    assert "1 步、合计 3 行" in page.text
    tz = logged.context.settings.server.display_tz
    assert moment_label(logged.runtime.last_prune_at, tz, seconds=True) in page.text
    assert logged.runtime.last_prune_at.isoformat() not in page.text


def test_health_page_shows_the_runtime_readout(logged):
    page = logged.client.get("/admin/health")
    assert page.status_code == 200
    assert "0.0.0" in page.text, "版本号要说清跑的是哪一份代码"
    assert "陈旧" not in page.text


def test_health_page_folds_the_intraday_moment_and_surfaces_every_error(logged):
    """C4/C5：读数是普通 dict，`stale` 可能缺席而换成了 `error`；盘中那一行有自己的 `ok`。"""
    ctx = logged.context
    poll = datetime(2026, 10, 23, 7, 30, tzinfo=UTC)
    logged.runtime.healthz = lambda: {
        "status": "degraded",
        "db": {"ok": False, "error": "库被锁住了"},
        "scheduler": {"beat_age_s": None, "error": "台账读不到"},
        "intraday": {
            "A": {"last_poll_at": poll.isoformat(), "age_s": 12, "source": "tencent", "ok": False},
            "HK": {"last_poll_at": None, "age_s": None, "source": None, "ok": True},
        },
        "version": "0.0.0+test",
        "uptime_s": 5,
    }
    page = logged.client.get("/admin/health")
    assert page.status_code == 200
    assert poll.isoformat() not in page.text, "ISO 原文不上屏：那是另一个时区的一串数字"
    assert moment_label(poll, ctx.settings.server.display_tz, seconds=True) in page.text
    assert "未轮询" in page.text
    assert "台账读不到" in page.text, "读不出调度器要说读不出，不能报一个空心跳当正常"
    assert "库被锁住了" in page.text
    assert "那一次轮询是失败的" in page.text, "轮询报过错的市场不许看着是健康的"


def test_channels_page_lists_every_owner_by_name(logged):
    """C7/C8：跨用户列通道，而「用户」那一列是人不是 UUID。"""
    ctx = logged.context
    alice = ctx.users.get_by_username("alice")
    bob = _make_bob(ctx)
    ctx.notify.add(bob.id, "wecom", "bob 的群", {"url": "https://example.invalid/hook"})
    ctx.notify.add(alice.id, "desktop", "本机弹窗", {})

    page = logged.client.get("/admin/channels")
    assert page.status_code == 200
    assert "bob 的群" in page.text and "本机弹窗" in page.text
    assert "bob" in page.text, "没有显示名就退回登录名"
    assert bob.id not in page.text and alice.id not in page.text


def test_channels_page_prefers_the_display_name(logged):
    ctx = logged.context
    bob = _make_bob(ctx)
    ctx.users.set_display_name(bob.id, "老李")
    ctx.notify.add(bob.id, "wecom", "群", {"url": "https://example.invalid/hook"})

    page = logged.client.get("/admin/channels")
    assert "老李" in page.text


def test_channels_page_never_shows_a_credential(logged):
    """配置在 `Channel` 里按构造就是掩码视图：这一条钉的是「跨用户读」不等于「读明文」。"""
    ctx = logged.context
    bob = _make_bob(ctx)
    ctx.notify.add(bob.id, "wecom", "群", {"url": "https://example.invalid/hook?key=abc123456789"})

    page = logged.client.get("/admin/channels")
    assert "abc123456789" not in page.text


def test_sources_page_toggle_writes_the_requested_state(logged):
    """停用/启用那一刀读的是隐藏 input 的值：`checked()` 只看键在不在，键永远在。

    用的是一条没有路由的源——「别让某个市场没有能取日线的源」那道闸只在有路由时才判，
    这一条要钉的是开关的读法，不是那道闸。少了它，屏幕上按「停用」而库里那条还是启用的。
    """
    ctx = logged.context
    ctx.data_sources.upsert(SourceRow(name="standalone", kind="sina", note="只看一眼的那条"))

    page = logged.client.get("/admin/sources")
    assert page.status_code == 200
    assert "standalone" in page.text and "只看一眼的那条" in page.text

    assert _post(
        logged.client, "/admin/sources/set_enabled", logged.csrf, name="standalone", enabled="0"
    ).status_code == 303
    assert ctx.data_sources.get("standalone").enabled is False

    assert _post(
        logged.client, "/admin/sources/set_enabled", logged.csrf, name="standalone", enabled="1"
    ).status_code == 303
    assert ctx.data_sources.get("standalone").enabled is True


def test_refusing_a_source_that_is_not_there_puts_that_sentence_on_the_page(logged):
    """被拒时念的就是服务那句话：页面重写第二套措辞，迟早分叉。"""
    resp = _post(logged.client, "/admin/sources/set_enabled", logged.csrf, name="nope", enabled="0")
    assert resp.status_code == 400
    assert "nope" in resp.text


def test_probe_reports_the_errors_it_already_has_words_for(logged):
    missing = _post(
        logged.client,
        "/admin/sources/probe",
        logged.csrf,
        name="nope",
        market="A",
        code="600519",
    )
    assert missing.status_code == 400
    assert "nope" in missing.text

    bad_date = _post(
        logged.client,
        "/admin/sources/probe",
        logged.csrf,
        name="nope",
        market="A",
        code="600519",
        start="去年今日",
    )
    assert bad_date.status_code == 400
