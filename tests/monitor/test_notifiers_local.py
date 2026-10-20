"""本机通道：`run` 注入件收的是 argv 列表，不是 shell 字符串。

拼接 shell 命令字符串等于把标的名称（来自外部数据源）送进一次 shell 解释：
一个名字叫 `x; rm -rf ~` 的票就是一次注入。argv 列表没有这个口（spec §12 的边界）。
"""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime

from sams.core.notify import AlertMessage
from sams.monitor.env import LocalEnv
from sams.monitor.notifiers import NotifierDeps
from sams.monitor.notifiers.desktop import DesktopNotifier
from sams.monitor.notifiers.voice import VoiceNotifier


class RunSpy:
    def __init__(self, returncode=0):
        self.calls: list[list[str]] = []
        self.returncode = returncode

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, self.returncode, stdout="", stderr="弹窗失败")


def deps_with(run, env):
    return NotifierDeps(
        post=lambda *a, **k: {}, mail=lambda *a, **k: None, run=run, env=env
    )


def msg(**over):
    base = {
        "alert_id": "a",
        "market": "A",
        "code": "601088",
        "name": "中国神华",
        "account_name": "主账户",
        "rule_name": "PR 低",
        "value_text": "PR=0.31",
        "threshold_text": "PR≤0.4",
        "severity": "strong",
        "stage": "入池",
        "triggered_at": datetime.now(UTC),
        "body": "",
    }
    base.update(over)
    return AlertMessage(**base)


def env_with(which, *, environ=None, platform="darwin"):
    """`platform` 必须给：这一整个文件要在同一台机器上把 mac 与 Linux 两支都走到，
    让 `os.uname()` 决定走哪支的话，CI 换台机器就有一半用例在测另一条代码。"""
    return LocalEnv.detect(
        environ={"PATH": "/usr/bin", **(environ or {})},
        which=which,
        platform=platform,
    )


MAC = env_with({"osascript": "/usr/bin/osascript", "say": "/usr/bin/say"})


def test_desktop_applescript_escapes_the_data_not_the_channel():
    run = RunSpy()
    notifier = DesktopNotifier({}, deps=deps_with(run, MAC))
    assert notifier.available()
    notifier.send(msg(name='神华"; rm -rf ~'))
    argv = run.calls[0]
    assert argv[0] == "/usr/bin/osascript" and argv[1] == "-e"
    assert len(argv) == 3  # 一条语句、一个参数：没有第二条 -e，也没有把命令拆成两次
    statement = argv[2]
    assert statement.count("display notification") == 1
    assert "rm -rf" in statement  # 恶意名字待在数据里，没有被剥掉
    assert r'神华\"' in statement  # 引号被转义，没能提前结束那个字符串
    assert statement.startswith('display notification "')
    assert statement.endswith('with title "sams 告警"')


def test_desktop_headline_is_trimmed_and_body_stays_out():
    """弹窗只有 200 字节的容器：正文（「为什么入池」那一段展开）留给手机侧通道，
    弹窗只负责说一句「有一条，去看看」。撑破容器不是显示不全，是 osascript 报错。"""
    run = RunSpy()
    DesktopNotifier({}, deps=deps_with(run, MAC)).send(msg(name="很长" * 200, body="补充" * 500))
    statement = run.calls[0][2]
    assert "补充" not in statement
    assert statement.endswith('with title "sams 告警"')
    assert statement.count("…") == 1
    wrapper = len('display notification "" with title "sams 告警"'.encode())
    assert len(statement.encode()) <= wrapper + 200


def test_voice_uses_say_with_the_short_text():
    run = RunSpy()
    result = VoiceNotifier({}, deps=deps_with(run, MAC)).send(msg())
    assert result.ok
    assert run.calls[0][0] == "/usr/bin/say"
    assert len(run.calls[0][-1].encode()) <= 140


def test_voice_reads_a_sentence_not_the_headline():
    """标题行里的 `|` 与 `[]` 在朗读里是噪音，念出来只会变成「竖线 方括号」。"""
    run = RunSpy()
    VoiceNotifier({}, deps=deps_with(run, MAC)).send(msg())
    spoken = run.calls[0][-1]
    assert "|" not in spoken and "[" not in spoken
    assert "中国神华" in spoken and "PR 低" in spoken


def test_voice_falls_back_to_osascript_when_say_is_missing():
    """`LocalEnv` 认为「有 osascript 就能出声」，这里就得真用 osascript：
    两边不一致的话，探测说可用、发出去是一次「找不到 say」。"""
    run = RunSpy()
    env = env_with({"osascript": "/usr/bin/osascript"})
    notifier = VoiceNotifier({}, deps=deps_with(run, env))
    assert notifier.available()
    notifier.send(msg())
    assert run.calls[0][0] == "/usr/bin/osascript"
    assert re.search(r'^say "', run.calls[0][2])


def test_remote_session_reports_unavailable_and_sends_nothing():
    run = RunSpy()
    env = LocalEnv.detect(
        environ={"SSH_TTY": "/dev/pts/0"},
        which={"osascript": "/usr/bin/osascript", "say": "/usr/bin/say"},
        platform="darwin",
    )
    for cls in (DesktopNotifier, VoiceNotifier):
        notifier = cls({}, deps=deps_with(run, env))
        assert not notifier.available()
        assert "远程" in notifier.unavailable_reason()
        result = notifier.send(msg())
        assert not result.ok and result.unavailable
    assert run.calls == []  # 不可用就不该起进程


def test_a_nonzero_exit_is_a_failure_not_an_unavailable():
    """子进程跑了但错了（弹窗服务没起）＝失败，该 mark_error；
    压根跑不了＝不可用，不该记失败。两者的屏幕措辞与健康页计数不同。"""
    run = RunSpy(returncode=1)
    result = DesktopNotifier({}, deps=deps_with(run, MAC)).send(msg())
    assert not result.ok
    assert not result.unavailable
    assert "退出码 1" in result.error


def test_a_missing_binary_is_reported_as_unavailable():
    def boom(argv, **kw):
        raise FileNotFoundError("No such file or directory: 'osascript'")

    result = DesktopNotifier({}, deps=deps_with(boom, MAC)).send(msg())
    assert not result.ok and result.unavailable


def test_linux_path_uses_notify_send():
    run = RunSpy()
    env = env_with(
        {"notify-send": "/usr/bin/notify-send"},
        environ={"DISPLAY": ":0"},
        platform="linux",
    )
    notifier = DesktopNotifier({}, deps=deps_with(run, env))
    assert notifier.available()
    notifier.send(msg())
    assert run.calls[0][0] == "/usr/bin/notify-send"
    assert run.calls[0][1] == "sams 告警"


def test_linux_without_display_is_unavailable():
    env = env_with({"notify-send": "/usr/bin/notify-send"}, platform="linux")
    notifier = DesktopNotifier({}, deps=deps_with(RunSpy(), env))
    assert not notifier.available()
    assert "图形" in notifier.unavailable_reason()


def test_linux_voice_is_never_available():
    env = env_with({"notify-send": "/usr/bin/notify-send"}, platform="linux")
    notifier = VoiceNotifier({}, deps=deps_with(RunSpy(), env))
    assert not notifier.available()
    assert "macOS" in notifier.unavailable_reason()


def test_escape_keeps_backslashes_readable():
    from sams.monitor.notifiers.desktop import escape

    assert escape('a"b') == 'a\\"b'
    assert escape("a\\b") == "a\\\\b"
