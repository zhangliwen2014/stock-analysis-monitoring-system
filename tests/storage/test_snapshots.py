"""快照仓库：同一天重采是整日覆盖，按日读回的是那一份完整的横截面。"""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl

from sams.datasource.spot import SPOT_SCHEMA
from sams.storage.repos.snapshots import SnapshotRepo

DAY = date(2026, 10, 28)
TAKEN = datetime(2026, 10, 28, 8, 41, tzinfo=UTC)


def frame(*codes: str, day: date = DAY) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "market": ["A"] * len(codes),
            "code": list(codes),
            "name": [f"票{code}" for code in codes],
            "quoted_on": [day] * len(codes),
            "price": [10.0] * len(codes),
            "pct_chg": [1.0] * len(codes),
            "amount": [1.0e8] * len(codes),
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


def test_replace_day_writes_and_reads_back(db):
    repo = SnapshotRepo(db)
    written = repo.replace_day(market="A", day=DAY, taken_at=TAKEN, frame=frame("600519", "000001"))
    assert written == 2
    read = repo.load_day("A", DAY)
    assert read.height == 2
    assert set(read["code"]) == {"600519", "000001"}
    assert read["taken_at"].unique().item() == TAKEN


def test_recollecting_the_same_day_overwrites_instead_of_adding(db):
    """同一天两个答案比没有答案更糟：页面上那句「今天这一篮」没法追溯。"""
    repo = SnapshotRepo(db)
    repo.replace_day(market="A", day=DAY, taken_at=TAKEN, frame=frame("600519", "000001"))
    repo.replace_day(market="A", day=DAY, taken_at=TAKEN, frame=frame("600519", "300750"))
    read = repo.load_day("A", DAY)
    assert set(read["code"]) == {"600519", "300750"}


def test_latest_day_picks_the_newest_and_says_none_when_empty(db):
    repo = SnapshotRepo(db)
    assert repo.latest_day("A") is None
    repo.replace_day(market="A", day=date(2026, 10, 27), taken_at=TAKEN, frame=frame("600519"))
    repo.replace_day(market="A", day=DAY, taken_at=TAKEN, frame=frame("600519"))
    assert repo.latest_day("A") == DAY
    assert repo.latest_day("HK") is None


def test_an_empty_frame_does_not_erase_history(db):
    repo = SnapshotRepo(db)
    repo.replace_day(market="A", day=DAY, taken_at=TAKEN, frame=frame("600519"))
    assert repo.replace_day(market="A", day=DAY, taken_at=TAKEN, frame=frame()) == 0
    assert repo.load_day("A", DAY).height == 1
