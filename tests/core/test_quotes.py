from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from sams.core.errors import ValidationError
from sams.core.quotes import (
    CLOSED,
    DELAYED,
    LIVE,
    NO_DATA,
    STALE,
    Quote,
    QuoteBatch,
    as_utc,
    quote_state,
)


def quote(**overrides) -> Quote:
    base = dict(
        market="A",
        code="600519",
        ts=datetime(2026, 10, 23, 2, 0, tzinfo=UTC),
        price=1500.0,
        prev_close=1490.0,
        source="tencent",
    )
    base.update(overrides)
    return Quote(**base)


def test_a_quote_refuses_a_price_that_is_not_a_traded_price():
    for bad in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValidationError, match="价格"):
            quote(price=bad)


def test_a_quote_requires_an_aware_timestamp():
    with pytest.raises(ValidationError, match="UTC"):
        quote(ts=datetime(2026, 10, 23, 10, 0))


def test_as_utc_attaches_the_market_zone_to_a_naive_source_time():
    naive = datetime(2026, 10, 23, 10, 0)
    assert as_utc(naive, ZoneInfo("Asia/Shanghai")) == datetime(2026, 10, 23, 2, 0, tzinfo=UTC)


def test_as_utc_respects_daylight_saving():
    naive = datetime(2026, 7, 1, 9, 30)
    assert as_utc(naive, ZoneInfo("America/New_York")) == datetime(2026, 7, 1, 13, 30, tzinfo=UTC)


def test_the_five_states_are_ordered_by_what_the_user_must_do_first():
    now = datetime(2026, 10, 23, 2, 0, 30, tzinfo=UTC)
    fresh = quote()
    assert quote_state(fresh, now=now, market_open=True, stale_s=60) == LIVE
    assert quote_state(None, now=now, market_open=True, stale_s=60) == NO_DATA
    assert quote_state(fresh, now=now, market_open=False, stale_s=60) == CLOSED
    old = quote(ts=datetime(2026, 10, 23, 1, 58, tzinfo=UTC))
    assert quote_state(old, now=now, market_open=True, stale_s=60) == STALE
    flagged = quote(is_stale=True)
    assert quote_state(flagged, now=now, market_open=True, stale_s=60) == STALE
    delayed = quote(delayed=True)
    assert quote_state(delayed, now=now, market_open=True, stale_s=60) == DELAYED
    late_delayed = quote(delayed=True, ts=datetime(2026, 10, 23, 1, 58, tzinfo=UTC))
    assert quote_state(late_delayed, now=now, market_open=True, stale_s=60) == STALE


def test_a_delayed_feed_is_not_measured_by_the_local_threshold():
    """实测 11-05：腾讯港股每一笔都晚 904 秒（`field30` 与墙上时钟），A 股晚 0~4 秒。

    60 秒是「本地多久没动算停更」，不该顺带把一路固定晚 15 分钟的源全天判成故障。
    """
    aged = quote(market="HK", code="03690", delayed=True, delay_s=900)
    now = datetime(2026, 10, 23, 2, 15, 4, tzinfo=UTC)
    assert quote_state(aged, now=now, market_open=True, stale_s=60) == DELAYED
    broken = quote(
        market="HK", code="03690", delayed=True, delay_s=900,
        ts=datetime(2026, 10, 23, 1, 54, tzinfo=UTC),
    )
    assert quote_state(broken, now=now, market_open=True, stale_s=60) == STALE


def test_the_allowance_comes_from_the_quote_not_from_the_market():
    """同一只票换到新浪那一路就是晚 30 分钟：延时是这一笔的出处，不是市场的属性。

    而 A 股那一路 `delay_s=0`，60 秒的本地闸一秒都不许放宽（放宽了就是拿旧价判止损）。
    """
    sina_hk = quote(market="HK", code="03690", delayed=True, delay_s=1800)
    at_29_minutes = datetime(2026, 10, 23, 2, 29, tzinfo=UTC)
    assert quote_state(sina_hk, now=at_29_minutes, market_open=True, stale_s=60) == DELAYED
    a_share = quote(ts=datetime(2026, 10, 23, 1, 58, tzinfo=UTC))
    assert quote_state(a_share, now=at_29_minutes, market_open=True, stale_s=60) == STALE


def test_a_quote_from_the_future_is_not_reported_as_negative_age():
    future = quote(ts=datetime(2026, 10, 23, 2, 1, tzinfo=UTC))
    now = datetime(2026, 10, 23, 2, 0, tzinfo=UTC)
    assert quote_state(future, now=now, market_open=True, stale_s=60) == LIVE


def test_an_empty_batch_is_still_a_batch():
    batch = QuoteBatch()
    assert batch.quotes == () and batch.skipped == () and batch.errors == ()


def test_a_naive_now_is_a_caller_bug_not_a_state():
    with pytest.raises(ValidationError, match="now"):
        quote_state(quote(), now=datetime(2026, 10, 23, 10, 0), market_open=True, stale_s=60)


def test_marked_stale_returns_a_new_quote_and_leaves_the_original():
    original = quote()
    flagged = original.marked_stale()
    assert flagged.is_stale is True
    assert original.is_stale is False
    assert flagged.ts == original.ts and flagged.price == original.price
