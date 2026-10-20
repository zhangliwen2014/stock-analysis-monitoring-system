"""PushPlus（手机 App 推送）：`token` 是唯一的凭据。

成功形状是 `code == 200`（不是 `errcode`），而它失败时也回一个 JSON。把「非空答复」
当成功，一个 code=400 的「token 无效」会被记成 sent——用户会以为手机没响是运营商的事。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sams.core.errors import ValidationError
from sams.core.notify import LIMITS, AlertMessage, NotifyResult, format_headline, trim
from sams.monitor.notifiers import NotifierDeps

ENDPOINT = "https://www.pushplus.plus/send"


def build_body(message: AlertMessage, token: str) -> dict[str, Any]:
    headline = format_headline(message)
    body = f"{headline}\n\n{message.body}" if message.body else headline
    return {
        "token": token,
        "title": trim(headline, limit=100),
        "content": trim(body, limit=LIMITS["pushplus"]),
        "template": "markdown",
    }


class PushPlusNotifier:
    name = "pushplus"

    def __init__(self, config: Mapping[str, Any], *, deps: NotifierDeps) -> None:
        self.token = str(config.get("token") or "").strip()
        if not self.token:
            raise ValidationError("PushPlus 通道需要 --config token=<官网的用户令牌>")
        self._deps = deps

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    def send(self, message: AlertMessage) -> NotifyResult:
        try:
            reply = self._deps.post(ENDPOINT, build_body(message, self.token), {})
        except Exception as exc:  # noqa: BLE001 — 异常类型不该越过 Notifier 协议
            return NotifyResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        code = reply.get("code") if reply else None
        if code == 200:
            return NotifyResult(ok=True)
        return NotifyResult(ok=False, error=f"PushPlus code={code} msg={reply.get('msg')}")
