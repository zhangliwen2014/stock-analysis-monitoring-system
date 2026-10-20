"""SMTP 通道：`mail` 注入件的形状决定了这一层能被测到什么程度。

注入的是「一封完整的邮件 + 一个配置」，而不是一个 SMTP 连接：测试要断言的是收件人、
主题、正文与 TLS 这三件，而不是「有没有调用 quit」。
"""

from __future__ import annotations

import smtplib
from datetime import UTC, datetime

import pytest

from sams.core.errors import ValidationError
from sams.core.notify import AlertMessage, format_headline
from sams.monitor.notifiers import NotifierDeps
from sams.monitor.notifiers.email import EmailNotifier

BASE = {
    "host": "smtp.qq.com",
    "port": 465,
    "user": "me@qq.com",
    "password": "app-code",
    "from": "me@qq.com",
    "to": ["you@example.com"],
}


def spy_mail(box):
    def send(config, to, subject, body):
        box.append((config, to, subject, body))

    return send


def make(config, box=None):
    box = box if box is not None else []
    n = EmailNotifier(
        {**BASE, **(config or {})},
        deps=NotifierDeps(
            post=lambda *a, **k: {}, mail=spy_mail(box), run=lambda *a, **k: None, env=None
        ),
    )
    return n, box


def message(**over):
    base = {
        "market": "A",
        "code": "601088",
        "name": "中国神华",
        "account_name": "主账户",
        "rule_name": "PR 低",
        "value_text": "PR=0.31",
        "threshold_text": "PR≤0.4",
        "severity": "weak",
        "stage": "入池",
        "triggered_at": datetime.now(UTC),
    }
    base.update(over)
    return AlertMessage(**base)


def test_subject_is_the_headline():
    n, box = make({})
    n.send(message())
    config, to, subject, body = box[0]
    assert "[A][主账户]" in subject
    assert "中国神华" in subject
    assert to == ["you@example.com"]
    assert config["host"] == "smtp.qq.com"
    assert body  # 正文不许是空的：收件箱列表里只显示主题，正文要等点开


def test_multiple_recipients_are_all_carried():
    n, box = make({"to": ["a@x.com", "b@y.com"]})
    n.send(message())
    assert box[0][1] == ["a@x.com", "b@y.com"]


def test_comma_separated_recipients_are_split():
    """CLI 给的是 `--config to=a@x.com,b@y.com`：一个串而不是列表，
    不拆的话「发给两个人」在屏幕上会变成「发给一个地址里带逗号的收件人」。"""
    n, box = make({"to": "a@x.com, b@y.com ,"})
    assert n.recipients == ["a@x.com", "b@y.com"]
    n.send(message())
    assert box[0][1] == ["a@x.com", "b@y.com"]


def test_body_falls_back_to_the_headline_when_there_is_no_extra_text():
    n, box = make({})
    n.send(message(body=""))
    assert box[0][3] == format_headline(message())
    n2, box2 = make({})
    n2.send(message(body="第二条是补充说明"))
    assert "补充说明" in box2[0][3]


def test_missing_host_is_refused_at_construction():
    with pytest.raises(ValidationError, match="host"):
        make({"host": ""})


def test_missing_password_is_refused_at_construction():
    with pytest.raises(ValidationError, match="授权码"):
        make({"password": ""})


def test_no_recipient_is_refused_at_construction():
    with pytest.raises(ValidationError, match="to"):
        make({"to": ""})


def test_bad_port_is_refused_at_construction():
    with pytest.raises(ValidationError, match="端口"):
        make({"port": "smtp"})


def test_ssl_defaults_to_the_port_convention():
    """465 隐式 SSL、587 走 STARTTLS：不写 `ssl` 时按端口推断，
    让用户少配一项，也让「换了端口忘了换 TLS」少一种发生方式。"""
    assert make({})[0].use_ssl is True
    assert make({"port": 587})[0].use_ssl is False
    assert make({"ssl": False})[0].use_ssl is False


def test_password_never_appears_in_the_error():
    """`SMTPAuthenticationError` 常把那一行请求吐进 message 里，直接 `str(exc)` 就等于
    把授权码写进 `notify_log.error`——那一列会被 `sams alerts` 打出来。"""

    def boom(config, to, subject, body):
        raise smtplib.SMTPAuthenticationError(535, b"login me@qq.com app-code rejected")

    n = EmailNotifier(
        BASE,
        deps=NotifierDeps(post=lambda *a, **k: {}, mail=boom, run=lambda *a, **k: None, env=None),
    )
    result = n.send(message())
    assert not result.ok
    assert "app-code" not in result.error
    assert "me@qq.com" not in result.error
    assert "授权码" in result.error


def test_a_network_failure_is_a_result_not_an_exception():
    def boom(config, to, subject, body):
        raise OSError("_connection refused")

    n = EmailNotifier(
        BASE,
        deps=NotifierDeps(post=lambda *a, **k: {}, mail=boom, run=lambda *a, **k: None, env=None),
    )
    result = n.send(message())
    assert not result.ok and "refused" in result.error
    assert not result.unavailable


def test_recipients_refused_carries_the_addresses():
    """收件人被拒时原文里的地址是有用的（哪个拼错了），密码不在其中，所以这一支可以回显。"""

    def boom(config, to, subject, body):
        raise smtplib.SMTPRecipientsRefused({"you@example.com": (550, b"no such user")})

    n = EmailNotifier(
        BASE,
        deps=NotifierDeps(post=lambda *a, **k: {}, mail=boom, run=lambda *a, **k: None, env=None),
    )
    result = n.send(message())
    assert not result.ok
    assert "you@example.com" in result.error


def test_subject_and_body_are_trimmed():
    n, box = make({})
    n.send(message(body="很长" * 9000))
    subject, body = box[0][2], box[0][3]
    assert len(subject.encode()) <= 120
    assert len(body.encode()) <= 20000
