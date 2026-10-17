from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from sams.core.errors import StorageError
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.symbols import SymbolRepo

D1, D2, D3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)


@pytest.fixture
def bars(db) -> BarRepo:
    return BarRepo(db)


@pytest.fixture
def symbols(db) -> SymbolRepo:
    return SymbolRepo(db)


def test_upsert_is_idempotent(bars, bars_frame):
    assert bars.upsert(bars_frame) == 3
    assert bars.upsert(bars_frame) == 3
    assert bars.count() == 3, "重跑同一区间不能长出重复行"


def test_same_key_updates_in_place(bars, bars_frame):
    bars.upsert(bars_frame)
    fixed = bars_frame.with_columns(
        pl.when(pl.col("date") == D2).then(9.9).otherwise(pl.col("close")).alias("close")
    )
    bars.upsert(fixed)
    assert bars.count() == 3
    loaded = bars.load("A", ["600519"], start=D2, end=D2)
    assert loaded["close"][0] == 9.9


def test_latest_date_and_empty_market(bars):
    assert bars.latest_date("A", "600519") is None
    bars.upsert(
        pl.DataFrame(
            {
                "market": ["A", "HK"],
                "code": ["600519", "00700"],
                "date": [D1, D3],
                "close": [1.0, 2.0],
                "source": ["akshare", "yfinance"],
            }
        )
    )
    assert bars.latest_date("A", "600519") == D1
    assert bars.latest_date("HK", "00700") == D3
    assert bars.latest_date("US", "AAPL") is None


def test_load_returns_range_ordered(bars, bars_frame):
    bars.upsert(bars_frame)
    rows = bars.load("A", ["600519"], start=D2)
    assert rows["date"].to_list() == [D2, D3]
    assert bars.load("A", []).is_empty()
    assert bars.count("A", "600519") == 3


def test_dirty_rows_are_dropped_not_stored(bars, bars_frame):
    dirty = bars_frame.with_columns(pl.lit(None, dtype=pl.Float64).alias("close"))
    assert bars.upsert(dirty) == 0
    assert bars.count() == 0


def test_missing_required_column_fails_loudly(bars):
    with pytest.raises(StorageError, match="close"):
        bars.upsert(pl.DataFrame({"market": ["A"], "code": ["1"]}))


def test_deduplicates_within_one_batch(bars, bars_frame):
    doubled = pl.concat([bars_frame, bars_frame.with_columns(pl.lit(0.0).alias("close"))])
    bars.upsert(doubled)
    assert bars.count() == 3


def test_symbol_upsert_fills_defaults(symbols):
    symbols.upsert(pl.DataFrame([{"market": "A", "code": "600519", "currency": "CNY"}]))
    row = symbols.get("A", "600519")
    assert row["list_status"] == "L" and row["lot_size"] == 1
    assert symbols.upsert(pl.DataFrame([{"market": "A", "code": "600519", "currency": "CNY"}])) == 1
    assert symbols.get("A", "missing") is None


def test_symbol_key_is_market_plus_code(symbols):
    symbols.upsert(
        pl.DataFrame(
            [
                {"market": "US", "code": "1", "currency": "USD", "name": "一品"},
                {"market": "HK", "code": "1", "currency": "HKD", "name": "一品港"},
            ]
        )
    )
    assert symbols.get("US", "1")["currency"] == "USD"
    assert symbols.get("HK", "1")["currency"] == "HKD"


def test_list_market_filters_delisted(symbols):
    symbols.upsert(
        pl.DataFrame(
            [
                {"market": "A", "code": "live", "currency": "CNY"},
                {"market": "A", "code": "gone", "currency": "CNY", "list_status": "D"},
            ]
        )
    )
    assert set(symbols.list_market("A")["code"]) == {"live"}
    assert set(symbols.list_market("A", listed_only=False)["code"]) == {"live", "gone"}
