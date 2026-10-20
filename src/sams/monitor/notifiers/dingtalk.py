"""钉钉群机器人：可选加签（`secret` 存在时按官方算法签 timestamp）。

签名的失败通常是时间戳漂移（服务器钟不准 → errcode=310000），那句话要写进 error 里，
因为「机器人被限流」与「这台机器钟不对」的修法完全不同。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
from collections.abc import Mapping
from typing import Any

from sams.core.errors import ValidationError
from sams.core.notify import LIMITS, AlertMessage, NotifyResult, format_headline, trim
from sams.monitor.notifiers import NotifierDeps


def signed_url(url: str, secret: str, *, now_s: float | None = None) -> str:
    timestamp = int((time.time() if now_s is None else now_s) * 1000)
    raw = f"{timestamp}\n{secret}".encode()
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}timestamp={timestamp}&sign={sign}"


def build_body(message: AlertMessage) -> dict[str, Any]:
    headline = format_headline(message)
    body = f"{headline}\n\n{message.body}" if message.body else headline
    return {
        "msgtype": "markdown",
        "markdown": {
            "title": trim(headline, limit=60),
            "text": trim(body, limit=LIMITS["dingtalk"]),
        },
    }


class DingTalkNotifier:
    name = "dingtalk"

    def __init__(self, config: Mapping[str, Any], *, deps: NotifierDeps) -> None:
        self.url = str(config.get("url") or "").strip()
        if not self.url:
            raise ValidationError("钉钉通道需要 --config url=<机器人 webhook>")
        self.secret = str(config.get("secret") or "").strip()
        self._deps = deps

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    def send(self, message: AlertMessage) -> NotifyResult:
        url = signed_url(self.url, self.secret) if self.secret else self.url
        try:
            reply = self._deps.post(
                url, build_body(message), {"Content-Type": "application/json"}
            )
        except Exception as exc:  # noqa: BLE001 — 异常类型不该越过 Notifier 协议
            return NotifyResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        code = reply.get("errcode") if reply else None
        if code == 0:
            return NotifyResult(ok=True)
        hint = "（若 errcode=310000：签名不匹配，先核对这台机器的时钟）" if self.secret else ""
        return NotifyResult(
            ok=False, error=f"钉钉 errcode={code} errmsg={reply.get('errmsg')}{hint}"
        )
