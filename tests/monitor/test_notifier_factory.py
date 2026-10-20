"""协议的形状与工厂的名单。

`Notifier` 只有三个方法（`available`/`unavailable_reason`/`send`）加一个 `name`。刻意没有
`probe()`：探针就是「发一条 weak 的合成消息」。有独立的 probe 实现，就等于每个通道多一条
只有探针走的代码路径，而「探针成功」这句话的意思会退化成「另一条路径没坏」。

各通道自己的用例在 T-11~T-13（`test_http_channels.py` / `test_email.py` /
`test_local_channels.py`）；本文件只测工厂本身，不碰任何一个真实出口。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sams.core.errors import ValidationError
from sams.core.notify import KINDS, AlertMessage
from sams.monitor.env import DESKTOP, VOICE, LocalEnv
from sams.monitor.notifiers import (
    Notifier,
    NotifierDeps,
    UnavailableNotifier,
    build_notifier_factory,
    default_deps,
    probe_message,
)

SSH = LocalEnv.detect(
    environ={"SSH_TTY": "/dev/pts/0"}, which={"osascript": "/x"}, platform="darwin"
)
NO_TOOLS = LocalEnv.detect(environ={"PATH": "/usr/bin"}, which={}, platform="darwin")


def deps(**kwargs):
    kwargs.setdefault("env", SSH)
    return NotifierDeps(
        post=lambda url, body, headers: {},
        mail=lambda config, to, subject, text: None,
        run=lambda argv, **kwargs2: None,
        **kwargs,
    )


def message(kind="mid"):
    return AlertMessage(market="A", code="601088", severity=kind, triggered_at=datetime.now(UTC))


def test_kinds_are_the_six_channels():
    assert set(KINDS) == {"wecom", "dingtalk", "pushplus", "email", "desktop", "voice"}


def test_unknown_kind_is_refused_with_the_list():
    """报错要名单全：拼错 `wangwei` 的人需要知道这里到底认识哪几个。"""
    with pytest.raises(ValidationError, match="wecom"):
        build_notifier_factory(deps=deps())("wangwei", {})


def test_kind_is_trimmed_and_lowered():
    assert build_notifier_factory(deps=deps())(f"  {VOICE.upper()} ", {}).name == VOICE


def test_a_local_channel_that_cannot_run_becomes_a_disabled_object_not_an_error():
    """factory 拿到 env 就要能把不可用的通道做成「禁用实现」而不是抛错：
    一个用户在远程终端上把 voice 配好了，规则不该因此整条发不出去。"""
    notifier = build_notifier_factory(deps=deps())(VOICE, {})
    assert isinstance(notifier, UnavailableNotifier)
    assert notifier.available() is False
    assert "远程" in notifier.unavailable_reason()


def test_a_local_channel_without_an_env_snapshot_is_refused():
    """`env=None` 只在 HTTP 通道那一侧合法：本机通道没有快照就是不知道该不该发。"""
    with pytest.raises(ValidationError, match="env"):
        build_notifier_factory(deps=deps(env=None))(DESKTOP, {})


def test_a_disabled_channel_reports_unavailable_rather_than_failed():
    """`send` 不抛、也不说「失败」：那一条会写成 `suppressed`，健康页不该记它一笔错误。"""
    result = build_notifier_factory(deps=deps())(VOICE, {}).send(message())
    assert not result.ok
    assert result.unavailable
    assert result.error


def test_the_protocol_is_three_methods_plus_a_name():
    """名单钉死在这里：第四个成员迟早会有人加（`probe` 是第一个），而每加一个就要在
    六个实现里各写一遍。"""
    assert Notifier.__protocol_attrs__ == {"name", "available", "unavailable_reason", "send"}


def test_build_without_deps_wires_the_real_world():
    """bootstrap 那一行不该被迫自己 import requests：不传 deps 就是真实装配。"""
    wired = default_deps()
    assert callable(wired.post) and callable(wired.mail) and callable(wired.run)
    assert isinstance(wired.env, LocalEnv)
    with pytest.raises(ValidationError, match="wangwei"):
        build_notifier_factory()("wangwei", {})


def test_probe_message_is_a_weak_one_about_the_channel_itself():
    probe = probe_message("wecom")
    assert isinstance(probe, AlertMessage)
    assert probe.severity == "weak"
    assert probe.market == "SYS"
    assert probe.code == "wecom"
    assert probe.stage == "探针"
