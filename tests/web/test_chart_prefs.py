"""图表偏好的词表：档名怎么排序、均线窗口怎么读。纯函数，不起 app。"""

import pytest

from sams.core.errors import ValidationError
from sams.web.chart_prefs import (
    CHART_PERIODS,
    ordered_periods,
    parse_periods,
    parse_windows,
    windows_text,
)

KEYS = tuple(name for name, _label in CHART_PERIODS)


def test_an_empty_pair_of_fields_means_the_defaults():
    assert parse_periods("", KEYS) == ()
    assert parse_windows("") == ()
    assert windows_text(()) == ""


def test_the_period_order_must_name_every_period_exactly_once():
    assert parse_periods(",".join(reversed(KEYS)), KEYS) == tuple(reversed(KEYS))
    with pytest.raises(ValidationError):
        parse_periods(",".join(KEYS[:-1]), KEYS)  # 少一档
    with pytest.raises(ValidationError):
        parse_periods(",".join([*KEYS, KEYS[0]]), KEYS)  # 多一档
    with pytest.raises(ValidationError):
        parse_periods("day,week,nope", KEYS)  # 不认识


def test_ma_windows_are_positive_unique_and_at_most_six():
    assert parse_windows(" 5, 10,20 ,5 ") == (5, 10, 20)
    assert windows_text((5, 10, 20)) == "5,10,20"
    for bad in ("0", "-5", "a", "1,2,3,4,5,6,7"):
        with pytest.raises(ValidationError):
            parse_windows(bad)


def test_a_stored_order_reorders_and_appends_periods_it_does_not_know():
    known = (("day", "日"), ("week", "周"), ("month", "月"))
    assert ordered_periods(known, ("month", "day")) == (
        ("month", "月"),
        ("day", "日"),
        ("week", "周"),
    )
    # 旧偏好里有一个已经下架的档：跳过它，不炸
    assert ordered_periods(known, ("gone", "week")) == (
        ("week", "周"),
        ("day", "日"),
        ("month", "月"),
    )
