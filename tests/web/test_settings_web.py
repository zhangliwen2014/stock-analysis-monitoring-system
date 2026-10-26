"""设置页：资料、密码（踢别的设备）、免打扰、通道维护、测试告警与探针限频。

这里断言的都是只有页面会弄错的事：状态码、哪一格进了库、谁的行被动过。
仓库与路由自己的规矩（密码长度、时区合法性、默认通道不能停用、参数的措辞）不在这里
重测一遍，只钉一句「页面念的是它那句话」——两处各写一套措辞迟早分叉。

不联网是这些用例能跑的前提：fixture 里 alice 一条通道都没有，所以
`deliver_existing` 在选路那一步就答 `no_channel`（不发送）；`wecom` 用的地址是
`example.invalid`，只落库。探针真的会发东西，所以那一段把 `requests.post` 换成替身：
留住的正是「点按钮 → 建工厂 → 打到那条 url → 结论回到这一屏」这根接线，
而退避重试那些细节仍由 monitor 层自己覆盖（`tests/monitor/test_router.py`）。
"""

from __future__ import annotations

import logging
from datetime import time

from sams.core.errors import DatabaseLocked
from sams.core.notify import SYSTEM_MARKET
from sams.storage.repos.channels import ChannelRepo

PASSWORD = "correct-horse-battery"
NEW_PASSWORD = "brand-new-password-1"


def _alice(logged):
    return logged.context.users.get_by_username("alice")


def _alice_id(logged) -> str:
    return _alice(logged).id


def _mine(logged):
    return logged.context.channels_store.list_for(_alice_id(logged))


def _only(logged):
    (channel,) = _mine(logged)
    return channel


def _add(logged, *, name="主群", kind="wecom", config="url=https://example.invalid/hook"):
    return logged.client.post(
        "/settings/channels",
        data={"csrf": logged.csrf, "kind": kind, "name": name, "config": config},
        follow_redirects=False,
    )


def _probe(logged, channel_id):
    return logged.client.post(
        f"/settings/channels/{channel_id}/probe",
        data={"csrf": logged.csrf},
        follow_redirects=False,
    )


def _stub_network(monkeypatch) -> list:
    """把探针与发送共用的那一个网络出口（`requests.post`）换成替身，记下它收到过的 url。

    换的是最外面那一层，所以工厂、正文构造、`errcode` 那一刀、`mark_ok` 全照真走。
    返回的替身对象带 `.json()`：`_real_post` 拿的就是 `requests` 的响应对象。
    """
    seen: list[str] = []

    class Reply:
        status_code = 200
        text = ""

        def json(self) -> dict:
            return {"errcode": 0, "errmsg": "ok"}

    def fake_post(url, **_kwargs):
        seen.append(url)
        return Reply()

    monkeypatch.setattr("requests.post", fake_post)
    return seen


def test_settings_page_renders(logged):
    page = logged.client.get("/settings")
    assert page.status_code == 200
    # 页面真正拥有的三样：这个人是谁、免打扰此刻是空的、还没有通道
    assert "登录名 alice" in page.text
    assert 'name="dnd_start" value=""' in page.text
    assert "还没有通道" in page.text


def test_profile_saves_display_name(logged):
    resp = logged.client.post(
        "/settings/profile",
        data={"csrf": logged.csrf, "display_name": "  爱丽丝  "},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _alice(logged).display_name == "爱丽丝"
    assert "爱丽丝" in logged.client.get("/settings").text


def test_profile_relays_the_repos_own_words(logged):
    """页面不自建第二套长度规矩：被拒时念的就是仓库那一句。"""
    resp = logged.client.post(
        "/settings/profile",
        data={"csrf": logged.csrf, "display_name": "x" * 41},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "显示名请控制在" in resp.text
    assert _alice(logged).display_name == ""


def test_password_change_keeps_this_device_and_drops_the_others(logged):
    ctx = logged.context
    other = ctx.sessions.issue(_alice_id(logged))  # 另一台设备
    assert ctx.sessions.verify(other) is not None

    wrong = logged.client.post(
        "/settings/password",
        data={"csrf": logged.csrf, "current": "not-my-password", "new": NEW_PASSWORD},
        follow_redirects=False,
    )
    assert wrong.status_code == 400
    assert ctx.sessions.verify(other) is not None, "当前密码不对：那是一句都没动"

    ok = logged.client.post(
        "/settings/password",
        data={"csrf": logged.csrf, "current": PASSWORD, "new": NEW_PASSWORD},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    assert ctx.users.authenticate("alice", PASSWORD) is None
    assert ctx.users.authenticate("alice", NEW_PASSWORD) is not None
    assert ctx.sessions.verify(other) is None, "改密码要让别的设备下线"
    # 而人站着的那一台不该被踢回登录页：`revoke_all` 做不到这一句
    assert logged.client.get("/settings").status_code == 200


def test_a_wrong_current_password_is_denied_in_the_log(logged, caplog):
    """改密码失败也要留一行审计：每一次「有人试过这台账号的密码」都值得回头看一眼。

    只留日志、**不**上锁或限速：这个会话已经能改显示名、清免打扰、动通道，多一道
    闸挡不住真正的攻击者，却可能把人锁在自己的改密页外面。
    """
    ctx = logged.context
    other = ctx.sessions.issue(_alice_id(logged))
    with caplog.at_level(logging.INFO, logger="sams.web"):
        resp = logged.client.post(
            "/settings/password",
            data={"csrf": logged.csrf, "current": "guess-me-not", "new": NEW_PASSWORD},
            follow_redirects=False,
        )
    assert resp.status_code == 400
    assert "deny settings.password" in caplog.text
    assert "current" in caplog.text, "那一句要说清失败的是哪一格"
    assert ctx.users.authenticate("alice", PASSWORD) is not None
    assert ctx.sessions.verify(other) is not None


def test_a_too_short_new_password_changes_nothing(logged):
    """密码长度的闸在仓库（`MIN_PASSWORD_LEN`），这里只转述；被拒时连会话都不该动。"""
    ctx = logged.context
    other = ctx.sessions.issue(_alice_id(logged))
    resp = logged.client.post(
        "/settings/password",
        data={"csrf": logged.csrf, "current": PASSWORD, "new": "short"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "密码至少" in resp.text
    assert ctx.sessions.verify(other) is not None
    assert ctx.users.authenticate("alice", PASSWORD) is not None


def test_dnd_saves_clears_and_rejects_partial(logged):
    save = logged.client.post(
        "/settings/dnd",
        data={
            "csrf": logged.csrf,
            "dnd_start": "23:00",
            "dnd_end": "07:00",
            "dnd_tz": "Asia/Shanghai",
        },
        follow_redirects=False,
    )
    assert save.status_code == 303
    user = _alice(logged)
    assert (user.dnd_start, user.dnd_end, user.dnd_tz) == (time(23), time(7), "Asia/Shanghai")
    # 框回填的是库里那一双，而且是 `<input type=time>` 收得下的写法（不带秒）
    assert 'value="23:00"' in logged.client.get("/settings").text

    clear = logged.client.post(
        "/settings/dnd",
        data={"csrf": logged.csrf, "dnd_start": "", "dnd_end": "", "dnd_tz": ""},
        follow_redirects=False,
    )
    assert clear.status_code == 303
    assert _alice(logged).dnd_tz is None

    partial = logged.client.post(
        "/settings/dnd",
        data={"csrf": logged.csrf, "dnd_start": "23:00", "dnd_end": "", "dnd_tz": ""},
        follow_redirects=False,
    )
    assert partial.status_code == 400
    assert "三个一起给" in partial.text


def test_a_window_stored_with_seconds_still_fills_the_box_as_hhmm(logged):
    """`<input type=time>` 收不下秒：库里那条带了秒，屏幕上也得折成 HH:MM。

    折不好不是「不好看」而是静默丢数据——浏览器把认不出的 value 当成没填，
    于是「打开看一眼」再按保存就成了一次清除窗口的提交。折算住在 `display.hhmm`。
    """
    ctx = logged.context
    ctx.users.set_dnd(
        _alice_id(logged), start=time(23, 0, 30), end=time(7, 30, 15), tz="Asia/Shanghai"
    )
    page = logged.client.get("/settings")
    assert 'name="dnd_start" value="23:00"' in page.text
    assert 'name="dnd_end" value="07:30"' in page.text
    assert "23:00:30" not in page.text


def test_dnd_rejects_a_zone_that_does_not_resolve(logged):
    """半开的窗口进不了库，拼错的时区也不行：进去了它就永远不匹配，而屏幕上看着是配好了。"""
    ctx = logged.context
    ctx.users.set_dnd(_alice_id(logged), start=time(23), end=time(7), tz="Asia/Shanghai")

    bad = logged.client.post(
        "/settings/dnd",
        data={
            "csrf": logged.csrf,
            "dnd_start": "23:00",
            "dnd_end": "07:00",
            "dnd_tz": "Asia/Shangai",
        },
        follow_redirects=False,
    )
    assert bad.status_code == 400
    assert "时区" in bad.text
    # 库里那一格还是原先那把：被拒的一次写入一个字都不该留下
    assert _alice(logged).dnd_tz == "Asia/Shanghai"

    wrong_clock = logged.client.post(
        "/settings/dnd",
        data={
            "csrf": logged.csrf,
            "dnd_start": "25:99",
            "dnd_end": "07:00",
            "dnd_tz": "Asia/Shanghai",
        },
        follow_redirects=False,
    )
    assert wrong_clock.status_code == 400
    assert "HH:MM" in wrong_clock.text


def test_channel_add_becomes_the_default_and_unknown_kind_never_lands(logged):
    assert _add(logged).status_code == 303
    channel = _only(logged)
    assert channel.kind == "wecom" and channel.is_default

    pigeon = _add(logged, name="鸽子", kind="carrier-pigeon")
    assert pigeon.status_code == 400
    assert "不认识通道类型" in pigeon.text
    assert len(_mine(logged)) == 1, "工厂先跑再落库：拼错的 kind 不该躺在库里"


def test_the_default_channel_cannot_be_disabled(logged):
    ctx = logged.context
    _add(logged)
    channel = _only(logged)
    stop = logged.client.post(
        f"/settings/channels/{channel.id}/disable", data={"csrf": logged.csrf},
        follow_redirects=False,
    )
    assert stop.status_code == 400
    assert "默认通道" in stop.text, "那句原因要跟着回列表页：兜底路由得落在能发的通道上"
    assert ctx.channels_store.show(channel.id).enabled is True


def test_second_channel_edits_disables_and_enables(logged):
    ctx = logged.context
    _add(logged, name="主群")
    _add(logged, name="备用", config="url=https://example.invalid/spare")
    spare = next(row for row in _mine(logged) if not row.is_default)

    edit = logged.client.post(
        f"/settings/channels/{spare.id}/edit",
        data={"csrf": logged.csrf, "config": "url=https://new.example/hook\nat_all=1"},
        follow_redirects=False,
    )
    assert edit.status_code == 303
    assert ctx.channels_store.credentials(spare.id) == {
        "url": "https://new.example/hook",
        "at_all": "1",
    }

    disable = logged.client.post(
        f"/settings/channels/{spare.id}/disable", data={"csrf": logged.csrf},
        follow_redirects=False,
    )
    assert disable.status_code == 303
    assert ctx.channels_store.show(spare.id).enabled is False
    enable = logged.client.post(
        f"/settings/channels/{spare.id}/enable", data={"csrf": logged.csrf},
        follow_redirects=False,
    )
    assert enable.status_code == 303
    assert ctx.channels_store.show(spare.id).enabled is True


def test_an_empty_box_on_the_edit_page_changes_nothing(logged):
    """空值=不动（编辑框永远不回显明文，人没法把旧值重打一遍）。"""
    ctx = logged.context
    _add(logged)
    channel = _only(logged)
    before = ctx.channels_store.credentials(channel.id)
    resp = logged.client.post(
        f"/settings/channels/{channel.id}/edit",
        data={"csrf": logged.csrf, "config": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert ctx.channels_store.credentials(channel.id) == before


_EMAIL_CONFIG = (
    "host=smtp.example.com\nuser=me@example.com\npassword=an-auth-code\nto=me@example.com"
)


def test_edit_refuses_a_config_the_factory_does_not_accept(logged):
    """改配置与新增走同一道闸：工厂先跑，跑不过就一个字都不落库。

    新增那一条本来就过工厂（`NotifyService.add`），编辑过去绕过它——于是一个拼错的
    `port` 会躺在库里，通道在选路时被静默跳过，人看到的是「配了却不发」。
    """
    ctx = logged.context
    _add(logged, name="邮箱", kind="email", config=_EMAIL_CONFIG)
    channel = _only(logged)
    before = ctx.channels_store.credentials(channel.id)

    resp = logged.client.post(
        f"/settings/channels/{channel.id}/edit",
        data={"csrf": logged.csrf, "config": "port=not-a-number"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "端口非法" in resp.text, "念的是工厂那句话，页面不自建第二套措辞"
    assert ctx.channels_store.credentials(channel.id) == before, "被拒的一次写入一个字都不留"


def test_the_edit_page_shows_keys_and_no_values(logged):
    """屏幕上只可能出现掩码视图：键名上屏（要让人认出哪一格配过），值一个都不上。
    `credentials()` 仍是明文唯一出口，页面上也没有「看一眼明文」那个开关。"""
    _add(logged, config="url=https://example.invalid/hook\nsecret_word=let-me-in-please")
    channel = _only(logged)
    page = logged.client.get(f"/settings/channels/{channel.id}/edit")
    assert page.status_code == 200
    assert "secret_word" in page.text
    assert "example.invalid" not in page.text
    assert "let-me-in-please" not in page.text


def test_a_config_line_without_an_equals_sign_is_refused(logged):
    resp = _add(logged, config="https://example.invalid/hook")
    assert resp.status_code == 400
    assert "等号" in resp.text
    assert _mine(logged) == []


def test_someone_elses_channel_is_404_before_it_is_touched(logged):
    """归属闸在写之前：别人的通道按 404 报，而那一行与发送一次都没被碰。"""
    ctx = logged.context
    bob = ctx.users.create("bob", "another-correct-horse")
    theirs = ctx.notify.add(bob.id, "wecom", "bob 的群", {"url": "https://example.invalid/bob"})
    before = ctx.channels_store.credentials(theirs.id)

    assert logged.client.get(f"/settings/channels/{theirs.id}/edit").status_code == 404
    edit = logged.client.post(
        f"/settings/channels/{theirs.id}/edit",
        data={"csrf": logged.csrf, "config": "url=https://attacker.example/hook"},
        follow_redirects=False,
    )
    assert edit.status_code == 404
    disable = logged.client.post(
        f"/settings/channels/{theirs.id}/disable", data={"csrf": logged.csrf},
        follow_redirects=False,
    )
    assert disable.status_code == 404
    probe = logged.client.post(
        f"/settings/channels/{theirs.id}/probe", data={"csrf": logged.csrf},
        follow_redirects=False,
    )
    assert probe.status_code == 404

    assert ctx.channels_store.credentials(theirs.id) == before
    assert ctx.channels_store.show(theirs.id).enabled is True
    assert ctx.channels_store.show(theirs.id).last_ok_at is None, "探针没被发出去过"


def test_database_locked_is_503_not_404(logged, monkeypatch):
    """库被调度线程握着时答 404 是一句自信的错误话：它会让人去重建一条本来就还在的通道。

    归属闸与启停那两处都兜 `SamsError`，而 `DatabaseLocked` 是它的子类——必须先冒出去。
    """

    def locked(self, channel_id, *, user_id=None):
        raise DatabaseLocked("sams.duckdb", detail="Conflicting lock (PID 42)")

    _add(logged)
    channel = _only(logged)
    monkeypatch.setattr(ChannelRepo, "show", locked)

    assert logged.client.get(f"/settings/channels/{channel.id}/edit").status_code == 503
    assert (
        logged.client.post(
            f"/settings/channels/{channel.id}/disable",
            data={"csrf": logged.csrf},
            follow_redirects=False,
        ).status_code
        == 503
    )
    assert (
        logged.client.post(
            "/settings/channels",
            data={
                "csrf": logged.csrf,
                "kind": "wecom",
                "name": "又被锁住的那条",
                "config": "url=https://example.invalid/second",
            },
            follow_redirects=False,
        ).status_code
        == 503
    )


def test_probe_on_my_own_channel_hits_that_url_and_reports_back(logged, monkeypatch):
    """接对了线那一句：按下去 → 建工厂 → 打到这条通道自己的 url → 结论回到这一屏。

    网络换成替身（`requests.post`），其余全真走：探针不写 `notify_log`，但它会
    `mark_ok`——那条通道在健康页上的状态确实变了，屏幕上要同时看得见。
    """
    ctx = logged.context
    seen = _stub_network(monkeypatch)
    _add(logged)
    channel = _only(logged)

    resp = _probe(logged, channel.id)
    assert resp.status_code == 200
    assert seen == ["https://example.invalid/hook"]
    assert "探针：主群" in resp.text, "那一块要带着通道名字：报告里没有 name"
    assert "sent" in resp.text
    assert ctx.channels_store.show(channel.id).last_ok_at is not None


def test_probe_is_rate_limited(logged, monkeypatch):
    """探针与测试告警一样会真发到手机上，而且一次跑三趟带退避：它也该有一张卡。

    窗口比测试告警短（30 秒）：排障时人要连着试几条，而连点依然是要挡的。
    """
    seen = _stub_network(monkeypatch)
    _add(logged)
    channel = _only(logged)

    assert _probe(logged, channel.id).status_code == 200
    second = _probe(logged, channel.id)
    assert second.status_code == 429
    assert "再等" in second.text
    assert len(seen) == 1, "那张卡挡在发送之前：被拒的一次不该再打一次网络"


def test_test_alert_is_recorded_and_rate_limited(logged):
    """成功不再整页重渲染（200 会被刷新重提交），303 回收件箱带那一句白名单提醒：
    「告警建了没有」仍由库里的行与投递记录钉，不由这一屏的 HTML。"""
    ctx = logged.context
    first = logged.client.post(
        "/settings/test-alert", data={"csrf": logged.csrf}, follow_redirects=False
    )
    assert first.status_code == 303
    assert first.headers["location"].startswith("/alerts?notice=")
    (row,) = ctx.alerts_store.list_for(_alice_id(logged))
    assert (row.market, row.code, row.is_test) == (SYSTEM_MARKET, "test", True)
    assert [item.outcome for item in ctx.alerts_store.deliveries(row.id)] == [
        "no_channel",
    ], "没有启用通道时选路在发送之前就拒绝"

    followed = logged.client.get(first.headers["location"])
    assert followed.status_code == 200
    assert "已发出这条测试告警" in followed.text

    second = logged.client.post(
        "/settings/test-alert", data={"csrf": logged.csrf}, follow_redirects=False
    )
    assert second.status_code == 429
    assert len(ctx.alerts_store.list_for(_alice_id(logged))) == 1, "限频挡在写库之前"
