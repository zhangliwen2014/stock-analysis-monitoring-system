"""SMTP 通道：`smtplib` 在全项目的唯一落点（T-2 的 GATED 就指着这个文件）。

授权码（QQ/163 的「SMTP 密码」）与登录失败的原因都不许出现在 `NotifyResult.error` 里：
那句话会被写进 `notify_log.error`、被 `sams alerts` 打出来、也可能被贴进截图。
"""

from __future__ import annotations

import smtplib
from collections.abc import Mapping
from email.message import EmailMessage
from typing import Any

from sams.core.errors import ValidationError
from sams.core.notify import LIMITS, AlertMessage, NotifyResult, format_headline, trim
from sams.monitor.notifiers import NotifierDeps


def _require(config: Mapping[str, Any]) -> tuple[str, int, str, str, str, list[str], bool]:
    host = str(config.get("host") or "").strip()
    if not host:
        raise ValidationError("邮件通道需要 --config host=smtp.example.com")
    try:
        port = int(config.get("port") or 465)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"邮件通道端口非法: {config.get('port')}") from exc
    user = str(config.get("user") or "").strip()
    password = str(config.get("password") or "").strip()
    if not user or not password:
        raise ValidationError(
            "邮件通道需要 --config user=… --config password=…："
            "这里的 password 是服务商给的**授权码**，不是登录密码"
        )
    sender = str(config.get("from") or user).strip()
    raw = config.get("to")
    recipients = (
        [str(item).strip() for item in raw if str(item).strip()]
        if isinstance(raw, (list, tuple))
        else [str(item).strip() for item in str(raw or "").split(",") if item.strip()]
    )
    if not recipients:
        raise ValidationError("邮件通道需要 --config to=收件人（逗号分隔或重复给）")
    use_ssl = bool(config.get("ssl", port == 465))
    return host, port, user, password, sender, recipients, use_ssl


class EmailNotifier:
    name = "email"

    def __init__(self, config: Mapping[str, Any], *, deps: NotifierDeps) -> None:
        self.config = dict(config)
        (
            self.host,
            self.port,
            self.user,
            self.password,
            self.sender,
            self.recipients,
            self.use_ssl,
        ) = _require(config)
        self._deps = deps

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    def render(self, message: AlertMessage) -> tuple[str, str]:
        headline = format_headline(message)
        body = message.body or headline
        # 主题就是标题行：收件箱列表里只显示主题，正文要等点开
        return trim(headline, limit=120), trim(body, limit=LIMITS["email"])

    def send(self, message: AlertMessage) -> NotifyResult:
        subject, body = self.render(message)
        try:
            self._deps.mail(self.config, list(self.recipients), subject, body)
        except Exception as exc:  # noqa: BLE001 — 异常类型不该越过 Notifier 协议
            return NotifyResult(ok=False, error=_describe(exc))
        return NotifyResult(ok=True)


def _describe(exc: BaseException) -> str:
    """把 SMTP 的异常说成人话，且**不带上密码**。

    `smtplib` 的 `SMTPAuthenticationError` 会把「用户名 密码」回显在 message 里，
    直接 `str(exc)` 就等于把授权码写进库里的一列文本。
    """
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "SMTP 登录被拒绝：授权码或账号不对（服务商通常要求用**授权码**而不是登录密码）"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return f"收件人被拒: {exc.recipients}"
    # 其余 SMTP 异常与网络异常同一句话：类型名 + 截断原文。截断是因为
    # `OSError` 的原文会带上完整 URL 与对端响应体，那比诊断有用更占屏幕。
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def send_mail(config: Mapping[str, Any], to: list[str], subject: str, body: str) -> None:
    """真实的 `NotifierDeps.mail`：465 走 SMTP_SSL，587 走 STARTTLS。

    两条都必须存在：只写一条的话，另一家服务商的配置会在屏幕上变成一个
    「连接被重置」，而那句错指向网络、真正的原因是没有 STARTTLS。
    """
    host = str(config["host"])
    port = int(config.get("port") or 465)
    user = str(config["user"])
    password = str(config["password"])
    sender = str(config.get("from") or user)
    use_ssl = bool(config.get("ssl", port == 465))

    letter = EmailMessage()
    letter["From"] = sender
    letter["To"] = ", ".join(to)
    letter["Subject"] = subject
    letter.set_content(body)

    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=20) as server:
            server.login(user, password)
            server.send_message(letter)
        return
    with smtplib.SMTP(host, port, timeout=20) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(user, password)
        server.send_message(letter)
