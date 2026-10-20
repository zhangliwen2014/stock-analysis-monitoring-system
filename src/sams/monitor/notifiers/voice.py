"""本机语音朗读（macOS `say`）。上限 140 字节：一段念 30 秒的告警没人会听完。

这是六个通道里唯一**必须靠文字长度控制**的一个：其它通道剪短是排版问题，
这里剪短是因为念到一半的那句话会被下一条打断。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sams.core.notify import LIMITS, AlertMessage, NotifyResult, trim
from sams.monitor.env import VOICE
from sams.monitor.notifiers import NotifierDeps
from sams.monitor.notifiers.desktop import _run_local, escape


class VoiceNotifier:
    name = "voice"

    def __init__(self, config: Mapping[str, Any], *, deps: NotifierDeps) -> None:
        self._deps = deps
        self._env = deps.env

    def available(self) -> bool:
        return bool(self._env and self._env.available(VOICE))

    def unavailable_reason(self) -> str:
        return self._env.reason(VOICE) if self._env else "这一层没有拿到本机环境。"

    def speak_text(self, message: AlertMessage) -> str:
        """念的是**能听的一句话**，不是标题行：`|` 与 `[]` 在朗读里是噪音。"""
        name = message.name or message.code
        return trim(
            f"{name} {message.rule_name} 触发，当前 {message.value_text}",
            limit=LIMITS["voice"],
        )

    def send(self, message: AlertMessage) -> NotifyResult:
        if not self.available():
            return NotifyResult(ok=False, error=self.unavailable_reason(), unavailable=True)
        env = self._env
        text = self.speak_text(message)
        say = env.found.get("say")
        if say:
            argv = [say, text]
        else:
            # `LocalEnv` 认「有 osascript 就能出声」，这里就得真用它：
            # 两边不一致的话，探测说可用、发出去是一次「找不到 say」
            argv = [env.found.get("osascript") or "osascript", "-e", f'say "{escape(text)}"']
        return _run_local(self._deps.run, argv)
