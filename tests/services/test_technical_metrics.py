"""个股页技术面读数（D356）：九个字段必须来自图表那条过闸序列。

这个文件钉三件事：

1. **同基**：卡片上的 MA20 与图上那条线（`SeriesRead.table` 的 `ma20` 末值）是同一个数，
   而不是另一套预热/复权口径凑出来的近似值；
2. **逐格说缺口**：30 根时 MA60 与 52 周位置没有数、MA5/MA20 照常有数；
3. **口径标注**：量比用不复权成交量，近 6 个交易日有送转/拆股时不给数。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from support import FrozenClock

from sams.services.analysis import AnalysisService
from sams.services.technical import VOLUME_SPLIT, symbol_metrics
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.corp_actions import CorpAction, CorpActionRepo
from sams.storage.repos.corp_actions_coverage import CorpActionsCoverageRepo
from sams.storage.repos.symbols import SymbolRepo

DAY = date(2026, 10, 28)
CODE = "600519"


def trading_days(n: int, *, end: date = DAY) -> list[date]:
    days: list[date] = []
    cursor = end
    while len(days) < n:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))


def seed(db, n: int = 70, *, volumes: list[float] | None = None) -> tuple[list[date], list[float]]:
    days = trading_days(n)
    closes = [100.0 + i * 0.5 for i in range(n)]
    volumes = volumes or [100.0] * n
    BarRepo(db).upsert(
        pl.DataFrame(
            {
                "market": ["A"] * n,
                "code": [CODE] * n,
                "date": days,
                "open": closes,
                "high": [value + 1 for value in closes],
                "low": [value - 1 for value in closes],
                "close": closes,
                "volume": volumes,
                "amount": [value * 100 for value in closes],
                "source": ["akshare"] * n,
            }
        )
    )
    return days, closes


def cover(db, *, start: date = date(2000, 1, 1), end: date = DAY) -> None:
    CorpActionsCoverageRepo(db).record(
        "A", CODE, covered_from=start, covered_to=end, full_history=True, src="akshare"
    )


def read(db, days: list[date]):
    service = AnalysisService(
        BarRepo(db),
        CorpActionRepo(db),
        CorpActionsCoverageRepo(db),
        SymbolRepo(db),
        clock=FrozenClock(datetime(2026, 10, 28, 9, tzinfo=UTC)),
    )
    return service.series(
        "A",
        CODE,
        start=days[0],
        end=DAY,
        indicators=("ma", "rsi"),
        windows={"ma": (5, 10, 20, 60)},
    )


def test_the_card_values_match_the_series_and_their_own_definitions(db):
    days, closes = seed(db, 70, volumes=[100.0] * 69 + [200.0])
    cover(db)
    result = read(db, days)

    metrics = symbol_metrics(result, CorpActionRepo(db).list_for("A", CODE, until=DAY))

    assert metrics.values["ma5"] == pytest.approx(sum(closes[-5:]) / 5)
    assert metrics.values["ma10"] == pytest.approx(sum(closes[-10:]) / 10)
    assert metrics.values["ma20"] == pytest.approx(sum(closes[-20:]) / 20)
    assert metrics.values["ma60"] == pytest.approx(sum(closes[-60:]) / 60)
    assert metrics.values["chg_20d"] == pytest.approx(closes[-1] / closes[-21] - 1)
    assert metrics.values["chg_60d"] == pytest.approx(closes[-1] / closes[-61] - 1)
    assert metrics.values["rsi14"] == pytest.approx(100.0)
    assert metrics.values["vol_ratio_5d"] == pytest.approx(2.0)
    # 70 根不够 52 周位置的 250 根：这一格必须自己说缺口，而不是悄悄留空
    assert metrics.values["pos_52w"] is None
    assert "日线不足 250 根" in metrics.reasons["pos_52w"]
    assert metrics.as_of == DAY
    assert metrics.written_at is not None


def test_the_card_moving_average_is_the_same_number_as_the_line_on_the_chart(db):
    """同一份 `SeriesRead` 出来的 MA20 与图表表头末值必须逐字相等（D356 同基）。"""
    days, _ = seed(db, 70)
    cover(db)
    result = read(db, days)
    chart_ma20 = result.table.sort("date").row(-1, named=True)["ma20"]

    metrics = symbol_metrics(result, CorpActionRepo(db).list_for("A", CODE, until=DAY))

    assert metrics.values["ma20"] == chart_ma20


def test_a_short_history_only_shortens_the_long_windows(db):
    days, closes = seed(db, 30)
    cover(db)
    result = read(db, days)

    metrics = symbol_metrics(result, CorpActionRepo(db).list_for("A", CODE, until=DAY))

    assert metrics.values["ma5"] == pytest.approx(sum(closes[-5:]) / 5)
    assert metrics.values["ma20"] == pytest.approx(sum(closes[-20:]) / 20)
    assert metrics.values["ma60"] is None
    assert "日线不足 60 根" in metrics.reasons["ma60"]
    assert metrics.values["pos_52w"] is None
    assert "日线不足 250 根" in metrics.reasons["pos_52w"]


def test_a_split_inside_the_volume_window_voids_the_ratio(db):
    days, _ = seed(db, 70)
    cover(db)
    CorpActionRepo(db).upsert(
        CorpAction(market="A", code=CODE, ex_date=days[-3], kind="split", ratio=2.0, src="akshare")
    )

    metrics = symbol_metrics(
        read(db, days), CorpActionRepo(db).list_for("A", CODE, until=DAY)
    )

    assert metrics.values["vol_ratio_5d"] is None
    assert metrics.reasons["vol_ratio_5d"] == VOLUME_SPLIT
