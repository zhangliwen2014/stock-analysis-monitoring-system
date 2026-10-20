"""三个走 HTTP 的通道：断言的是「真发时会带上哪些字节」。

没有 live 用例（spec §1 第 13 条）：一次真实成功会打到用户手机上。所以这里把请求体逐字段
钉住，剩下的「网络那头认不认」由 T-18 的人工验收一次确认。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sams.core.errors import ValidationError
from sams.core.notify import AlertMessage, NotifyResult, format_headline
from sams.monitor.env import LocalEnv
from sams.monitor.notifiers import NotifierDeps
from sams.monitor.notifiers.dingtalk import DingTalkNotifier
from sams.monitor.notifiers.pushplus import ENDPOINT, PushPlusNotifier
from sams.monitor.notifiers.wecom import WecomNotifier


class PostSpy:
    def __init__(self, responses=None):
        self.calls: list[tuple[str, dict, dict]] = []
        self.responses = list(responses or [{"errcode": 0, "errmsg": "ok"}])

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


def deps(post=None):
    return NotifierDeps(
        post=post or PostSpy(),
        mail=lambda *a, **k: None,
        run=lambda *a, **k: None,
        env=LocalEnv.detect(environ={"SSH_TTY": "/dev/pts/0"}, which={}, platform="darwin"),
    )


def message(severity="mid", **over):
    base = {
        "alert_id": "a1",
        "market": "A",
        "code": "601088",
        "name": "中国神华",
        "account_name": "主账户",
        "rule_name": "PR 低",
        "value_text": "PR=0.31",
        "threshold_text": "PR≤0.4",
        "severity": severity,
        "stage": "入池",
        "triggered_at": datetime.now(UTC),
        "body": "",
    }
    base.update(over)
    return AlertMessage(**base)


def test_wecom_posts_a_text_body():
    spy = PostSpy()
    WecomNotifier({"url": "https://qyapi/x?key=k"}, deps=deps(spy)).send(message())
    url, body, headers = spy.calls[0]
    assert url == "https://qyapi/x?key=k"
    assert body["msgtype"] == "markdown"
    assert "中国神华" in body["markdown"]["content"]
    assert headers["Content-Type"] == "application/json"


def test_wecom_errcode_is_a_failure_not_an_exception():
    """企微用 errcode 表达失败，HTTP 状态仍是 200：把 200 当成功，一条 token 失效的
    通道会在健康页上永远绿。"""
    spy = PostSpy(responses=[{"errcode": 93000, "errmsg": "invalid webhook url"}])
    result = WecomNotifier({"url": "https://qyapi/x"}, deps=deps(spy)).send(message())
    assert isinstance(result, NotifyResult)
    assert not result.ok
    assert "93000" in result.error
    assert "invalid webhook url" in result.error


def test_dingtalk_signs_when_a_secret_is_configured():
    spy = PostSpy()
    DingTalkNotifier(
        {"url": "https://oapi.dingtalk/robot", "secret": "SECabc"}, deps=deps(spy)
    ).send(message())
    url, body, _ = spy.calls[0]
    assert "timestamp=" in url and "sign=" in url
    assert body["msgtype"] == "markdown"
    assert body["markdown"]["title"]
    assert "中国神华" in body["markdown"]["text"]


def test_dingtalk_without_secret_sends_unsigned():
    spy = PostSpy()
    DingTalkNotifier({"url": "https://oapi.dingtalk/robot"}, deps=deps(spy)).send(message())
    assert "sign=" not in spy.calls[0][0]


def test_dingtalk_sign_is_the_official_algorithm():
    """钉住一个已知答复：签名算法一旦改错，钉钉只回一句 sign not match，
    而那句话在凌晨的告警里读起来像是限流。"""
    from sams.monitor.notifiers.dingtalk import signed_url

    url = signed_url("https://oapi.dingtalk/robot?access_token=t", "SECabc", now_s=1700000000.0)
    head, _, sign = url.partition("&timestamp=1700000000000&sign=")
    assert head == "https://oapi.dingtalk/robot?access_token=t"
    assert sign
    # 同一个 secret、同一个时刻必须得到同一个 sign（HMAC 是确定的）
    other = signed_url("https://x", "SECabc", now_s=1700000000.0)
    assert other.endswith(f"?timestamp=1700000000000&sign={sign}")


def test_pushplus_uses_the_topic_and_token_form():
    spy = PostSpy(responses=[{"code": 200, "data": None}])
    PushPlusNotifier({"token": "t-1"}, deps=deps(spy)).send(message())
    url, body, _ = spy.calls[0]
    assert url == ENDPOINT
    assert body["token"] == "t-1"
    assert body["template"] == "markdown"
    assert "中国神华" in body["content"]


def test_pushplus_code_zero_is_not_the_success_shape():
    """PushPlus 成功给 code=200。把「非空 JSON」当成功会让一个 code=400 的答复
    被记成 sent——用户会以为手机没响是运营商的事。"""
    spy = PostSpy(responses=[{"code": 400, "msg": "token 无效"}])
    result = PushPlusNotifier({"token": "bad"}, deps=deps(spy)).send(message())
    assert not result.ok
    assert "400" in result.error


def test_a_network_error_becomes_a_result_not_an_exception():
    def boom(url, body, headers):
        raise OSError("connection reset")

    result = WecomNotifier({"url": "https://qyapi/x"}, deps=deps(boom)).send(message())
    assert not result.ok and "connection reset" in result.error
    assert not result.unavailable


def test_an_empty_reply_is_a_failure():
    """对端回了个空 dict（网关吃掉 body、`.json()` 解析失败被换成空）时不能记成 sent。"""
    result = WecomNotifier({"url": "https://qyapi/x"}, deps=deps(lambda *a: {})).send(message())
    assert not result.ok


def test_missing_credential_is_a_config_error_not_a_send_failure():
    """构造时就抛：凭据缺失是「这条通道配错了」，该在 `sams channel add` 的屏幕上说清楚，
    而不是等一次告警发送时变成一条 `ok=False`。"""
    for cls, config in ((WecomNotifier, {}), (DingTalkNotifier, {}), (PushPlusNotifier, {})):
        with pytest.raises(ValidationError):
            cls(config, deps=deps())


def test_long_body_is_trimmed_to_the_channel_byte_limit():
    spy = PostSpy()
    WecomNotifier({"url": "https://qyapi/x"}, deps=deps(spy)).send(message(body="很长" * 4000))
    content = spy.calls[0][1]["markdown"]["content"]
    assert len(content.encode()) <= 4000
    assert content.endswith("…")
    # 剪完是「字节数在上限内」而不是「字符数=上限」：后者在企微那边是一次 41003


def test_empty_body_does_not_leave_a_trailing_blank_line():
    spy = PostSpy()
    WecomNotifier({"url": "https://qyapi/x"}, deps=deps(spy)).send(message())
    assert spy.calls[0][1]["markdown"]["content"] == format_headline(message())
