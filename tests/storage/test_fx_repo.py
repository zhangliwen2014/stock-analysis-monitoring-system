"""汇率仓库：给不出就说给不出，别用 0 顶。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from sams.core.errors import ValidationError
from sams.storage.db import Database
from sams.storage.repos.fx import FxRate, FxRepo

DAY = date(2024, 10, 25)


@pytest.fixture
def fx(db: Database) -> FxRepo:
    return FxRepo(db)


def test_upsert_then_get(fx: FxRepo) -> None:
    fx.put(DAY, "USD", "CNY", 7.1, source="yfinance")
    row = fx.get(DAY, "USD", "CNY")
    assert row.rate == pytest.approx(7.1)
    assert row.is_stale is False


def test_missing_rate_is_none_not_zero(fx: FxRepo) -> None:
    """把「没有汇率」读成 0 会让整段市值归零，而 0 看起来像一个合法的数。"""
    assert fx.get(DAY, "EUR", "CNY") is None


def test_the_same_currency_needs_no_row(fx: FxRepo) -> None:
    """CNY 账户里的 CNY 持仓换 1:1，这是正常的路径，不是缺数降级。"""
    row = fx.get(DAY, "CNY", "CNY")
    assert row.rate == pytest.approx(1.0)
    assert row.is_stale is False


def test_the_newest_rate_on_or_before_the_day_is_found(fx: FxRepo) -> None:
    fx.put(date(2024, 10, 23), "USD", "CNY", 7.0, source="yfinance")
    fx.put(date(2024, 10, 24), "USD", "CNY", 7.05, source="yfinance")
    row = fx.latest(date(2024, 10, 25), "USD", "CNY")
    assert row.day == date(2024, 10, 24)
    assert row.rate == pytest.approx(7.05)
    # 不是当天就标陈旧：`age_days` 才是「几天」的出处，两者不合成一个布尔
    assert row.is_stale is True
    assert row.age_days == 1


def test_a_rate_older_than_the_stale_window_is_not_returned(fx: FxRepo) -> None:
    fx.put(date(2024, 10, 1), "USD", "CNY", 7.0, source="yfinance")
    assert fx.latest(date(2024, 10, 25), "USD", "CNY") is None


def test_a_rate_inside_the_window_is_returned_and_flagged(fx: FxRepo) -> None:
    """周五到周一：3 天，可以用，但必须看得出来不是当天的数。"""
    fx.put(date(2024, 10, 18), "USD", "CNY", 7.0, source="yfinance")
    row = fx.latest(date(2024, 10, 21), "USD", "CNY")
    assert row.rate == pytest.approx(7.0)
    assert row.is_stale is True
    assert row.age_days == 3


def test_the_window_can_be_widened_by_the_caller(fx: FxRepo) -> None:
    """长假第七天仍然要一个数时，由调用方明确说「放宽到 10 天」，不由仓库默认放行。"""
    old = DAY - timedelta(days=8)
    fx.put(old, "USD", "CNY", 7.0, source="yfinance")
    assert fx.latest(DAY, "USD", "CNY") is None
    assert fx.latest(DAY, "USD", "CNY", max_age_days=10).day == old


def test_latest_returns_none_when_nothing_is_that_far_back(fx: FxRepo) -> None:
    assert fx.latest(date(2020, 1, 1), "USD", "CNY") is None


def test_the_same_day_can_be_written_twice_only_once(fx: FxRepo) -> None:
    """同日重同步是常态（补数据、换源），后写覆盖前写并记下来源。"""
    fx.put(DAY, "USD", "CNY", 7.1, source="yfinance")
    fx.put(DAY, "USD", "CNY", 7.12, source="manual")
    assert fx.get(DAY, "USD", "CNY").rate == pytest.approx(7.12)
    assert fx.get(DAY, "USD", "CNY").source == "manual"


def test_a_non_positive_rate_is_refused(fx: FxRepo) -> None:
    for bad in (0.0, -7.0, float("inf"), float("nan")):
        with pytest.raises(ValidationError, match="汇率"):
            fx.put(DAY, "USD", "CNY", bad, source="manual")


def test_a_rate_is_never_its_own_inverse(fx: FxRepo) -> None:
    """只存 (base, quote) 方向，读 USD→CNY 不能顺手返回 CNY→USD 的倒数。

    自动取倒数的便利会让「方向写反」变成一个大约对、永远查不出的错。
    """
    fx.put(DAY, "USD", "CNY", 7.1, source="yfinance")
    assert fx.get(DAY, "CNY", "USD") is None
    assert fx.latest(DAY, "CNY", "USD") is None


def test_storing_a_same_currency_rate_is_refused(fx: FxRepo) -> None:
    """1.0 是推导出来的，存下来就多一个会被人改错的数。"""
    with pytest.raises(ValidationError, match="同币种"):
        fx.put(DAY, "CNY", "CNY", 1.0, source="manual")


def test_a_stored_flag_survives_being_read_on_its_own_day(fx: FxRepo) -> None:
    """库里 `is_stale` 是采集那一刻写下的（降级取到的数），当天读也要看得见。"""
    fx.put(DAY, "USD", "CNY", 7.1, source="eastmoney", is_stale=True)
    row = fx.get(DAY, "USD", "CNY")
    assert row.is_stale is True
    assert isinstance(row, FxRate)
