"""企业微信群机器人：POST 一个 markdown 体，`errcode == 0` 才算成功。

HTTP 200 不是成功——企微用 errcode 表达失败（token 失效也回 200）。把 200 当成功，
一条坏掉的通道会在健康页上永远绿，而用户以为自己在收告警。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sams.core.errors import ValidationError
from sams.core.notify import LIMITS, AlertMessage, NotifyResult, format_headline, trim
from sams.monitor.notifiers import NotifierDeps


def build_body(message: AlertMessage) -> dict[str, Any]:
    headline = format_headline(message)
    body = f"{headline}\n\n{message.body}" if message.body else headline
    return {"msgtype": "markdown", "markdown": {"content": trim(body, limit=LIMITS["wecom"])}}


def outcome(reply: Mapping[str, Any]) -> tuple[bool, str]:
    if not reply:
        return False, "企微返回了一个空答复"
    code = reply.get("errcode")
    if code == 0:
        return True, ""
    return False, f"企微 errcode={code} errmsg={reply.get('errmsg')}"


class WecomNotifier:
    name = "wecom"

    def __init__(self, config: Mapping[str, Any], *, deps: NotifierDeps) -> None:
        self.url = str(config.get("url") or "").strip()
        if not self.url:
            raise ValidationError(
                "企微通道需要 --config url=<群机器人 webhook>："
                "webhook 里带着 key，所以它本身就是凭据"
            )
        self._deps = deps

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    def send(self, message: AlertMessage) -> NotifyResult:
        try:
            reply = self._deps.post(
                self.url, build_body(message), {"Content-Type": "application/json"}
            )
        except Exception as exc:  # noqa: BLE001 — 网络层五花八门，router 只认 NotifyResult
            return NotifyResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        ok, error = outcome(reply)
        return NotifyResult(ok=ok, error=error)
