from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift, RateLimited, TransientError
from sams.datasource.base import BAR_COLUMNS, classify_error, code_in_market, normalize_bars

MAP = {
    "date": "日期",
    "open": "开盘",
    "high": "最高",
    "low": "最低",
    "close": "收盘",
    "volume": "成交量",
    "amount": "成交额",
}


def raw(**overrides) -> pl.DataFrame:
    base = {
        "日期": ["20240102", "20240103", "20240104"],
        "开盘": [1.0, 2.0, 3.0],
        "最高": [1.5, 2.5, 3.5],
        "最低": [0.5, 1.5, 2.5],
        "收盘": [1.2, 2.2, 3.2],
        "成交量": [100, 200, 300],
        "成交额": [120, 440, 960],
    }
    base.update(overrides)
    return pl.DataFrame(base)


def normalize(frame: pl.DataFrame):
    return normalize_bars(frame, market="A", code="600519", source="akshare", column_map=MAP)


def test_standard_columns_and_identity_columns():
    out = normalize(raw())
    assert out.columns == BAR_COLUMNS
    assert out["market"].unique().to_list() == ["A"]
    assert out["code"].unique().to_list() == ["600519"]
    assert out["source"].unique().to_list() == ["akshare"]
    assert out["date"].dtype == pl.Date
    assert out["close"].dtype == pl.Float64


def test_yyyymmdd_and_iso_and_datetime_all_parse():
    expected = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    for values in (
        ["2024-01-02", "2024-01-03", "2024-01-04"],
        ["2024-01-02 00:00:00", "2024-01-03 00:00:00", "2024-01-04 00:00:00"],
        ["2024-01-02T00:00:00", "2024-01-03T00:00:00", "2024-01-04T00:00:00"],
    ):
        assert normalize(raw(**{"日期": values}))["date"].to_list() == expected, values


def test_native_date_and_datetime_columns_parse():
    import datetime as dt

    out = normalize(raw(**{"日期": [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]}))
    assert out["date"].to_list() == [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]

    stamped = pl.DataFrame(
        {
            "日期": [dt.datetime(2024, 1, 2, 15, 30), dt.datetime(2024, 1, 3)],
            "收盘": [1.0, 2.0],
        }
    )
    assert normalize(stamped)["date"].to_list() == [date(2024, 1, 2), date(2024, 1, 3)]


def test_datetime_with_timezone_suffix_still_resolves():
    """akshare 某些列会给到 '2024-01-02 00:00:00 CST'，前 10 位仍是合法日期。"""
    suffixed = [f"2024-01-0{d} 00:00:00 CST" for d in (2, 3, 4)]
    out = normalize(raw(**{"日期": suffixed}))
    assert out["date"].to_list() == [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]


def test_rows_are_sorted_ascending():
    out = normalize(raw(**{"日期": ["20240104", "20240102", "20240103"]}))
    assert out["date"].to_list() == [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]


def test_same_day_duplicates_collapse_to_the_last_row():
    duplicate = raw().with_columns(pl.lit(0.0).alias("收盘"))
    out = normalize(pl.concat([raw(), duplicate.head(1)]))
    assert out.height == 3, "同一天重复行必须折叠成一条"
    first = out.filter(pl.col("date") == date(2024, 1, 2))["close"][0]
    assert first == 0.0, "keep=last：后出现的修订值覆盖前一行"


def test_optional_column_missing_becomes_null_not_failure():
    """免费源少给一列成交额是常态，不该因此丢整天数据。"""
    frame = raw().drop("成交额")
    out = normalize(frame)
    assert out["amount"].null_count() == 3
    assert out.height == 3


def test_required_column_missing_raises_field_drift():
    with pytest.raises(FieldDrift, match="收盘"):
        normalize(raw().drop("收盘"))
    with pytest.raises(DatasourceError):
        normalize(raw().drop("日期"))


def test_rows_without_close_are_dropped_with_count():
    frame = raw(**{"收盘": [1.2, None, 3.2]})
    out = normalize(frame)
    assert out["date"].to_list() == [date(2024, 1, 2), date(2024, 1, 4)]
    assert out.height == 2


def test_all_rows_invalid_raises_empty_response():
    with pytest.raises(EmptyResponse):
        normalize(raw(**{"收盘": [None, None, None]}))


@pytest.mark.parametrize(
    "market, code, symbol",
    [
        ("US", "aapl", "AAPL"),
        ("US", "BRK.B", "BRK.B"),
        ("HK", "00700", "00700.HK"),
        ("HK", "700", "00700.HK"),
        ("HK", "hk00700", "00700.HK"),
        ("A", "600519", "600519"),
    ],
)
def test_code_in_market(market, code, symbol):
    assert code_in_market(market, code) == symbol


def test_code_in_market_rejects_blank():
    for bad in ("", "   ", None):
        with pytest.raises(ValueError):
            code_in_market("A", bad)


@pytest.mark.parametrize(
    "message, expected",
    [
        ("HTTP 429 Too Many Requests", RateLimited),
        ("请求过于频繁", RateLimited),
        ("抱歉，您发出的请求过频", RateLimited),
        ("rate limit exceeded", RateLimited),
        ("connection reset by peer", TransientError),
        ("timed out", TransientError),
        ("KeyError: 'close'", DatasourceError),
    ],
)
def test_classify_error(message, expected):
    exc = classify_error("eastmoney", RuntimeError(message))
    assert isinstance(exc, expected)
    assert exc.source == "eastmoney"


def test_oserror_and_connectionerror_instances_are_transient():
    assert isinstance(classify_error("akshare", TimeoutError("x")), TransientError)
    assert isinstance(classify_error("akshare", ConnectionError("x")), TransientError)
