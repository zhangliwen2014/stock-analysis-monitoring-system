"""快照服务：票池来自代码表，源答不出的逐个点名，日从报价时刻来。"""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest
from support import FrozenClock

from sams.core.errors import EmptyResponse, ValidationError
from sams.datasource.registry import AdapterRegistry
from sams.datasource.spot import SPOT_SCHEMA
from sams.services.market_snapshot import MarketSnapshotService
from sams.storage.repos.snapshots import SnapshotRepo
from sams.storage.repos.symbol_list import SymbolListRepo

NOW = datetime(2026, 10, 28, 8, 41, tzinfo=UTC)


def spot_rows(*codes: str, quoted: date | None = date(2026, 10, 28)):
    return pl.DataFrame(
        {
            "market": ["A"] * len(codes),
            "code": list(codes),
            "name": [f"票{c}" for c in codes],
            "quoted_on": [quoted] * len(codes),
            "price": [10.0] * len(codes),
            "pct_chg": [1.0] * len(codes),
            "amount": [1e8] * len(codes),
            "turnover_rate": [1.0] * len(codes),
            "volume_ratio": [1.0] * len(codes),
            "amplitude": [1.0] * len(codes),
            "pe_ttm": [10.0] * len(codes),
            "pe_dyn": [9.0] * len(codes),
            "pb": [1.2] * len(codes),
            "market_cap": [100.0] * len(codes),
            "float_cap": [90.0] * len(codes),
            "is_st": [False] * len(codes),
            "source": ["tencent"] * len(codes),
        },
        schema=SPOT_SCHEMA,
    )


class SnapStub:
    name = "tencent"
    markets = frozenset({"A"})
    market_snapshot_markets = frozenset({"A"})

    def __init__(self, frame):
        self.frame = frame

    def market_snapshot(self, market, codes):
        return self.frame


class FramelessStub(SnapStub):
    def market_snapshot(self, market, codes):
        raise EmptyResponse("tencent", "空")


def service(db, frame):
    registry = AdapterRegistry(
        adapters={"tencent": SnapStub(frame)},
        market_sources={"A": ("tencent",)},
        sleep=lambda _s: None,
    )
    return MarketSnapshotService(
        registry,
        SymbolListRepo(db),
        SnapshotRepo(db),
        clock=FrozenClock(NOW),
    )


def seed_codes(db, *codes):
    SymbolListRepo(db).upsert_batch(
        "A",
        [
            {"code": c, "name": c, "py_initials": c, "py_full": c, "currency": "CNY"}
            for c in codes
        ],
    )


def test_universe_comes_from_the_code_table_and_missing_rows_are_named(db):
    seed_codes(db, "600519", "000001", "300750")
    sync = service(db, spot_rows("600519", "300750")).sync("A")
    assert sync.universe_size == 3
    assert sync.rows == 2
    assert sync.skipped == ("000001",)
    assert sync.day == date(2026, 10, 28)


def test_the_day_comes_from_the_quote_moments_not_from_the_wall_clock(db):
    """节假日补采时，今天没有报价：日要落在最近那个有报价的交易日。"""
    seed_codes(db, "600519")
    sync = service(db, spot_rows("600519", quoted=date(2026, 10, 23))).sync("A")
    assert sync.day == date(2026, 10, 23)


def test_an_empty_code_table_is_a_refusal_with_the_next_command(db):
    with pytest.raises(ValidationError, match="symbol_master"):
        service(db, spot_rows()).sync("A")


def test_nothing_is_written_when_every_source_confirms_empty(db):
    seed_codes(db, "600519")
    clock = FrozenClock(NOW)
    registry = AdapterRegistry(
        adapters={"tencent": FramelessStub(None)},
        market_sources={"A": ("tencent",)},
        sleep=lambda _s: None,
    )

    sync = MarketSnapshotService(
        registry, SymbolListRepo(db), SnapshotRepo(db), clock=clock
    ).sync("A")
    assert sync.rows == 0 and sync.empty_everywhere
    assert SnapshotRepo(db).latest_day("A") is None
