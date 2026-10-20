"""桌面弹窗与语音朗读的可用性判定。

断言的是**判定表**，不是本机真相：`environ`/`which`/`platform` 三个入参让 mac 支与
Linux 支在同一台开发机上都被执行到。不打这张表的话，Linux 那一支第一次被执行是在
一台弹不出东西的机器上。
"""

from __future__ import annotations

from sams.monitor.env import DESKTOP, VOICE, LocalEnv

SSH = {"SSH_TTY": "/dev/pts/0"}
SSH_CONN = {"SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22"}
TMUX = {"TMUX": "/tmp/tmux-501/default,1234,0"}
LOCAL = {"PATH": "/usr/bin:/bin"}
MAC_TOOLS = {"osascript": "/usr/bin/osascript", "say": "/usr/bin/say"}
LINUX_TOOLS = {"notify-send": "/usr/bin/notify-send"}
WITH_DISPLAY = {"PATH": "/usr/bin", "DISPLAY": ":0"}


def mac(**kwargs):
    kwargs.setdefault("which", MAC_TOOLS)
    kwargs.setdefault("environ", LOCAL)
    return LocalEnv.detect(platform="darwin", **kwargs)


def linux(**kwargs):
    kwargs.setdefault("which", LINUX_TOOLS)
    kwargs.setdefault("environ", WITH_DISPLAY)
    return LocalEnv.detect(platform="linux", **kwargs)


def test_an_ssh_session_has_neither_desktop_nor_voice():
    env = mac(environ=SSH)
    assert not env.available(DESKTOP)
    assert not env.available(VOICE)
    assert "远程会话" in env.reason(DESKTOP)


def test_ssh_connection_and_tmux_also_count_as_remote():
    assert not mac(environ=SSH_CONN).available(VOICE)
    assert not mac(environ=TMUX).available(DESKTOP)


def test_a_local_mac_with_osascript_can_do_both():
    env = mac()
    assert env.available(DESKTOP)
    assert env.available(VOICE)
    assert env.reason(DESKTOP) == ""
    assert env.reason(VOICE) == ""


def test_osascript_alone_is_enough_for_voice_on_a_mac():
    """`say` 不在也能念：osascript 的 `say` 命令是第二条真能出声的路。"""
    assert mac(which={"osascript": "/usr/bin/osascript"}).available(VOICE)


def test_a_missing_binary_is_the_reason():
    env = mac(which={})
    assert not env.available(DESKTOP)
    assert "osascript" in env.reason(DESKTOP)
    assert "osascript" in env.reason(VOICE)


def test_linux_needs_notify_send_and_a_display():
    assert linux().available(DESKTOP), "Linux 走 notify-send，有 DISPLAY 就算能弹"
    blocked = linux(environ=LOCAL)
    assert not blocked.available(DESKTOP)
    assert "DISPLAY" in blocked.reason(DESKTOP)


def test_linux_names_no_macos_binary():
    """「我的 Linux 为什么说缺 osascript」这种问题不该发生：两支各只列自己那一条路。"""
    reason = linux(which={}).reason(DESKTOP)
    assert "notify-send" in reason
    assert "osascript" not in reason


def test_voice_has_no_linux_path():
    env = linux()
    assert not env.available(VOICE)
    assert "macOS" in env.reason(VOICE)


def test_every_reason_ends_with_an_actionable_fix():
    for kind, env in (
        (DESKTOP, mac(environ=SSH)),
        (VOICE, mac(environ=SSH)),
        (DESKTOP, mac(which={})),
        (DESKTOP, linux(environ=LOCAL)),
        (VOICE, linux()),
    ):
        reason = env.reason(kind)
        assert reason == reason.strip(), kind
        assert reason.endswith("。"), f"{kind} 的原因要以一句可执行的建议收尾"
        assert "通道" in reason, f"{kind} 的原因要说出换哪条通道"


def test_an_unknown_kind_is_refused_rather_than_defaulted():
    env = mac()
    assert not env.available("wecom")
    assert "wecom" in env.reason("wecom")


def test_detecting_the_real_machine_never_raises():
    """不传任何参数就走真实机器：远程终端里 PATH 上没有那些二进制是常态，不是错误。"""
    env = LocalEnv.detect()
    assert isinstance(env.available(DESKTOP), bool)
    if not env.available(DESKTOP):
        assert env.reason(DESKTOP), "禁用必须说得出为什么，否则健康页只能写「不知为何」"
