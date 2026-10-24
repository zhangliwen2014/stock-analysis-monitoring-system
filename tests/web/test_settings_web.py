"""设置页：资料、密码（踢别的设备）、免打扰、通道维护、测试告警限频。

这里断言的都是只有页面会弄错的事：状态码、哪一格进了库、谁的行被动过。
仓库与路由自己的规矩（密码长度、时区合法性、默认通道不能停用、参数的措辞）不在这里
重测一遍，只钉一句「页面念的是它那句话」——两处各写一套措辞迟早分叉。

不联网是这些用例能跑的前提：fixture 里 alice 一条通道都没有，所以
`deliver_existing` 在选路那一步就答 `no_channel`（不发送）；`wecom` 用的地址是
`example.invalid`，只落库。探针真的会发东西，那段行为由 monitor 层自己的测试覆盖
（`tests/monitor/test_router.py`），这里只钉它接的是哪根线、以及归属闸挡在它前面。
"""

from __future__ import annotations

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


def test_settings_page_renders(logged):
    assert logged.client.get("/settings").status_code == 200


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


def test_test_alert_is_recorded_and_rate_limited(logged):
    ctx = logged.context
    first = logged.client.post(
        "/settings/test-alert", data={"csrf": logged.csrf}, follow_redirects=False
    )
    assert first.status_code == 200
    (row,) = ctx.alerts_store.list_for(_alice_id(logged))
    assert (row.market, row.code, row.is_test) == (SYSTEM_MARKET, "test", True)
    assert [item.outcome for item in ctx.alerts_store.deliveries(row.id)] == [
        "no_channel",
    ], "没有启用通道时选路在发送之前就拒绝"
    assert "no_channel" in first.text

    second = logged.client.post(
        "/settings/test-alert", data={"csrf": logged.csrf}, follow_redirects=False
    )
    assert second.status_code == 429
    assert len(ctx.alerts_store.list_for(_alice_id(logged))) == 1, "限频挡在写库之前"
