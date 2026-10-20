"""本机桌面通知：macOS 走 `osascript`，Linux 走 `notify-send`。

argv 是列表而不是字符串，因为标的名称来自外部数据源：一个叫 `x; rm -rf ~` 的票名
如果被拼进 shell 字符串，就是一次注入。`subprocess.run(..., shell=False)` + 列表参数
让那个名字只能是一条数据。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sams.core.notify import LIMITS, AlertMessage, NotifyResult, format_headline, trim
from sams.monitor.env import DESKTOP
from sams.monitor.notifiers import NotifierDeps

TITLE = "sams 告警"


def escape(text: str) -> str:
    """AppleScript 的字面量转义：反斜杠与双引号。

    转义是为了**保留**内容而不是过滤掉它：被剥掉分号的标题行会让用户读到一个
    与告警当时不同的话。
    """
    return text.replace("\\", "\\\\").replace('"', '\\"')


class DesktopNotifier:
    name = "desktop"

    def __init__(self, config: Mapping[str, Any], *, deps: NotifierDeps) -> None:
        self._deps = deps
        self._env = deps.env

    def available(self) -> bool:
        return bool(self._env and self._env.available(DESKTOP))

    def unavailable_reason(self) -> str:
        return self._env.reason(DESKTOP) if self._env else "这一层没有拿到本机环境。"

    def send(self, message: AlertMessage) -> NotifyResult:
        if not self.available():
            return NotifyResult(ok=False, error=self.unavailable_reason(), unavailable=True)
        text = trim(format_headline(message), limit=LIMITS["desktop"])
        env = self._env
        if env.platform == "darwin":
            argv = [
                env.found.get("osascript") or "osascript",
                "-e",
                f'display notification "{escape(text)}" with title "{TITLE}"',
            ]
        else:
            argv = [env.found.get("notify-send") or "notify-send", TITLE, text]
        return _run_local(self._deps.run, argv)


def _run_local(run, argv: list[str]) -> NotifyResult:
    """`run` 抛异常＝这台机器上跑不了（不可用），退出码非零＝跑了但错了（失败）。

    这两件事在健康页上要分开：前者是「请改用手机的通道」，后者是「这条通道坏了，去看日志」。
    """
    try:
        done = run(argv)
    except Exception as exc:  # noqa: BLE001 — 协议不许进程异常越界
        return NotifyResult(ok=False, error=f"{type(exc).__name__}: {exc}", unavailable=True)
    if getattr(done, "returncode", 0) != 0:
        stderr = (done.stderr or "").strip()[:200]
        return NotifyResult(ok=False, error=f"{argv[0]} 退出码 {done.returncode}：{stderr}")
    return NotifyResult(ok=True)
