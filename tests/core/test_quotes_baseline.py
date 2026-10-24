"""cross_baseline 的三个分支与两个词汇函数：上一轮同日才算「上一根」。"""

from __future__ import annotations

from datetime import UTC, datetime

from sams.core.quotes import (
    MARKET_ZONES,
    Quote,
    cross_baseline,
    local_day,
    source_label,
)

ZONE = MARKET_ZONES["A"]


def quote(**over):
    base = {
        "market": "A",
        "code": "601088",
        "ts": datetime(2026, 10, 23, 3, 0, tzinfo=UTC),
        "price": 9.4,
        "source": "tencent",
    }
    base.update(over)
    return Quote(**base)


def test_same_day_previous_is_the_baseline():
    earlier = quote(ts=datetime(2026, 10, 23, 2, 0, tzinfo=UTC), price=9.95)
    assert cross_baseline(quote(), earlier, zone=ZONE) == 9.95


def test_yesterdays_round_is_not_today_first_jump():
    # 跨了日的那一轮是昨天的行情：拿它当「上一根」，今天第一次穿过就永远不会报
    yesterday = quote(ts=datetime(2026, 10, 22, 6, 0, tzinfo=UTC), price=9.95)
    assert cross_baseline(quote(), yesterday, zone=ZONE) is None


def test_no_previous_is_none():
    assert cross_baseline(quote(), None, zone=ZONE) is None


def test_local_day_uses_the_market_zone():
    # UTC 03:00 = 上海 11:00；UTC 19:00 = 上海次日 03:00——日子跟着市场走
    assert local_day(datetime(2026, 10, 23, 3, 0, tzinfo=UTC), ZONE).isoformat() == "2026-10-23"
    assert local_day(datetime(2026, 10, 23, 19, 0, tzinfo=UTC), ZONE).isoformat() == "2026-10-24"


def test_source_label_prefers_the_chinese_name():
    assert source_label("tencent") == "腾讯"
    assert source_label("sina_quote") == "新浪"
    assert source_label("mystery") == "mystery"
    assert source_label("") == "未知源"
