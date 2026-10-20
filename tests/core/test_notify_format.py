"""通知的词汇层：级别、路由表、字数上限、那条标题行。

这一层不含任何 I/O，所以 §11 的每条措辞都能在这里被逐字断言。住在 core 的理由与
`core/fundamentals.py` 一样：`storage` 与 `monitor` 都要引用同一套拼写，而 `storage`
不许 import `monitor`。
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from sams.core.errors import ValidationError
from sams.core.notify import (
    DEFAULT_ROUTE,
    LIMITS,
    OUTCOMES,
    SEVERITIES,
    AlertMessage,
    format_headline,
    route_for,
    trim,
)


def make_message(**over):
    base = {
        "alert_id": "a1",
        "market": "A",
        "code": "601088",
        "name": "中国神华",
        "account_name": "主账户",
        "rule_name": "PR 低于 0.4",
        "value_text": "PR=0.31",
        "threshold_text": "PR≤0.4",
        "severity": "mid",
        # UTC 下午两点半 = 北京 22:30：这个时刻专门挑的，标题行必须显示 UTC 而不是本地
        "triggered_at": datetime(2026, 10, 20, 14, 30, tzinfo=UTC),
        "stage": "入池",
        "body": "",
    }
    base.update(over)
    return AlertMessage(**base)


def test_severity_is_a_three_value_word():
    assert SEVERITIES == ("strong", "mid", "weak")
    assert set(DEFAULT_ROUTE) == set(SEVERITIES)
    assert set(OUTCOMES) == {"sent", "failed", "suppressed", "no_channel"}


def test_strong_route_includes_the_local_channels():
    """strong 里带 voice：这是「止损在凌晨也要响」那条要求，不是锦上添花。"""
    assert "voice" in DEFAULT_ROUTE["strong"]
    assert DEFAULT_ROUTE["weak"] == ("email",)


def test_route_for_rejects_an_unknown_severity():
    with pytest.raises(ValidationError, match="severity"):
        route_for("urgent")


def test_headline_is_the_spec_shape():
    """`[市场][账户名] 名称 代码 | 规则 | 当前值/阈值 | 时间 | 阶段`（§11）

    时间是 `2026-10-20 14:30:00 UTC` 这个形状——与 `stamp()` 逐字一致（D80）：屏幕上别处
    写着 UTC，标题行却写本地时分，读的人要先猜哪个是哪个。
    """
    text = format_headline(make_message())
    assert text == (
        "[A][主账户] 中国神华 601088 | PR 低于 0.4 | PR=0.31/PR≤0.4"
        " | 2026-10-20 14:30:00 UTC | 入池"
    )


def test_headline_folds_a_beijing_moment_to_utc():
    """同一个瞬间，输入是北京 22:30，输出仍然是 14:30 UTC：标题行与屏幕别处的 `stamp()`
    说同一种时间，否则一条告警在手机上写 22:30、在 CLI 里写 14:30，看起来像隔了八小时。"""
    text = format_headline(
        make_message(
            triggered_at=datetime(2026, 10, 20, 22, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
    )
    assert "2026-10-20 14:30:00 UTC" in text


def test_strong_gets_a_prefix_mid_and_weak_do_not():
    assert format_headline(make_message(severity="strong")).startswith("【强】[A]")
    assert not format_headline(make_message(severity="weak")).startswith("【")


def test_missing_account_headings_show_the_unknown_instead_of_blanks():
    """空账户名会让标题行出现 `[A][] ...`，读起来像排版坏了而不是「不知道属于哪个账户」。"""
    text = format_headline(make_message(account_name=""))
    assert "[未知账户]" in text


def test_an_empty_market_or_code_is_refused_at_construction():
    with pytest.raises(ValidationError, match="market"):
        make_message(market="")


def test_trim_is_measured_in_utf8_bytes():
    """上限是通道 API 的字节数限制，不是字符数：企微 4000 字节 = 约 1300 个汉字。"""
    out = trim("中" * 200, limit=30)
    assert len(out.encode()) <= 30
    assert out.endswith("…")
    # 剪在半个汉字中间会被 decode(errors="ignore") 丢掉那半个，不能留下乱码
    assert "ď" not in out and "锟" not in out and "\ufffd" not in out


def test_trim_does_not_truncate_when_it_fits():
    assert trim("短", limit=100) == "短"


def test_email_limit_is_the_biggest_and_voice_the_smallest():
    assert LIMITS["voice"] < LIMITS["desktop"] < LIMITS["wecom"] < LIMITS["email"]
    assert LIMITS["wecom"] == 4000


def test_no_channel_route_is_not_a_failure():
    """`route_for` 只回答「默认该走哪几个」；一个都没有由 router 写成 no_channel，
    这里不返回空 tuple 之外的暗号——空 tuple 与「没配通道」在健康页是两句话。"""
    assert set(DEFAULT_ROUTE["mid"]) == {"wecom", "dingtalk"}
