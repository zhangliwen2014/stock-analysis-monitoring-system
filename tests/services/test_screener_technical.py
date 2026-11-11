"""技术面字段（S4-A / D251–D253）：在快照日为止的复权日线上现算，算不出的票点名。

三条结构性的断言：

1. 读数只到**快照日**：快照日之后的 K 线即使已经入库，也不能改写那一天的 MA/RSI；
2. **覆盖闸优先**：没确认过公司行为的票记「无数」并给同步命令，绝不按裸价兜底；
3. 短窗口字段照算、长窗口字段各自点名缺口：不能因为 MA250 缺数就把 MA20 一起抹掉。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from sams.core.errors import ValidationError
from sams.core.screener_fields import Condition, fields_for, validate
from sams.services.screener import ScreenerService
from sams.services.technical import TechnicalService
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.corp_actions_coverage import CorpActionsCoverageRepo
from sams.storage.repos.financials import FinancialRepo
from sams.storage.repos.snapshots import SnapshotRepo
from sams.storage.repos.symbol_list import SymbolListRepo
from sams.storage.repos.symbols import SymbolRepo

DAY = date(2026, 10, 28)
TAKEN = datetime(2026, 10, 28, 8, 41, tzinfo=UTC)
CODE = "600519"


def trading_days(n: int, *, end: date = DAY) -> list[date]:
    days: list[date] = []
    cursor = end
    while len(days) < n:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))


def _snap(code=CODE):
    return {
        "code": code,
        "name": f"票{code}",
        "quoted_on": DAY,
        "price": 229.5,
        "pct_chg": 0.5,
        "amount": 1e8,
        "turnover_rate": 1.0,
        "volume_ratio": 1.0,
        "amplitude": 1.0,
        "pe_ttm": 10.0,
        "pe_dyn": 10.0,
        "pb": 1.0,
        "market_cap": 100.0,
        "float_cap": 90.0,
        "is_st": False,
        "source": "tencent",
    }


def seed_snapshot(db, codes=(CODE,)):
    from sams.datasource.spot import SPOT_SCHEMA

    SymbolListRepo(db).upsert_batch(
        "A",
        [
            {"code": c, "name": c, "py_initials": c, "py_full": c, "currency": "CNY"}
            for c in codes
        ],
    )
    frame = pl.DataFrame(
        [{"market": "A", "taken_at": TAKEN, **_snap(c)} for c in codes],
        schema={
            "market": pl.Utf8,
            "taken_at": pl.Datetime("us", "UTC"),
            **SPOT_SCHEMA,
        },
    )
    SnapshotRepo(db).replace_day(market="A", day=DAY, taken_at=TAKEN, frame=frame)


def seed_bars(db, *, n=260, code=CODE, volumes: list[float] | None = None, extra=None):
    days = trading_days(n)
    closes = [100.0 + i * 0.5 for i in range(n)]
    volumes = volumes or [100.0] * n
    data = {
        "market": ["A"] * n,
        "code": [code] * n,
        "date": days,
        "open": closes,
        "high": [value + 1 for value in closes],
        "low": [value - 1 for value in closes],
        "close": closes,
        "volume": volumes,
        "amount": [value * 100 for value in closes],
        "source": ["akshare"] * n,
    }
    if extra:
        for key, values in extra.items():
            data[key] = values
    BarRepo(db).upsert(pl.DataFrame(data))
    return days, closes


def seed_coverage(db, *, code=CODE, start=date(2000, 1, 1), end=DAY):
    CorpActionsCoverageRepo(db).record(
        "A", code, covered_from=start, covered_to=end, full_history=True, src="akshare"
    )


def service(db):
    return ScreenerService(
        SnapshotRepo(db),
        SymbolListRepo(db),
        FinancialRepo(db),
        technical=TechnicalService(
            BarRepo(db),
            CorpActionRepo(db),
            CorpActionsCoverageRepo(db),
            SymbolRepo(db),
        ),
    )


def by_code(result, code=CODE):
    return next(hit for hit in result.hits if hit.code == code)


def reasons(result, field: str) -> list[str]:
    return [group.reason for group in result.missing if group.field == field]


def test_technical_fields_are_computed_on_adjusted_closes_up_to_the_snapshot_day(db):
    seed_snapshot(db)
    seed_bars(db, volumes=[100.0] * 259 + [200.0])
    seed_coverage(db)
    # 快照日之后的一根大阳线：如果读数越过快照日，MA 与 RSI 都会被它改写
    BarRepo(db).upsert(
        pl.DataFrame(
            {
                "market": ["A"],
                "code": [CODE],
                "date": [DAY + timedelta(days=1)],
                "open": [1000.0],
                "high": [1001.0],
                "low": [999.0],
                "close": [1000.0],
                "volume": [100.0],
                "amount": [1e5],
                "source": ["akshare"],
            }
        )
    )
    result = service(db).run(
        "A",
        [
            Condition("ma20_gap", ">=", 0.02),
            Condition("ma60_gap", ">=", 0),
            Condition("ma250_gap", ">=", 0),
            Condition("rsi14", ">=", 50),
            Condition("vol_ratio_5d", ">=", 1.0),
            Condition("pos_52w", ">=", 0.5),
        ],
        day=DAY,
    )

    assert result.total_hits == 1
    hit = by_code(result)
    closes = [100.0 + i * 0.5 for i in range(260)]
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60
    ma250 = sum(closes[-250:]) / 250
    assert hit.values["ma20_gap"] == pytest.approx(closes[-1] / ma20 - 1)
    assert hit.values["ma60_gap"] == pytest.approx(closes[-1] / ma60 - 1)
    assert hit.values["ma250_gap"] == pytest.approx(closes[-1] / ma250 - 1)
    assert hit.values["rsi14"] == pytest.approx(100.0)
    assert hit.values["vol_ratio_5d"] == pytest.approx(2.0)
    assert hit.values["pos_52w"] == pytest.approx(1.0)


def test_technical_fields_are_not_offered_for_hk_or_us():
    names = {item.name for item in fields_for("HK")}
    assert "ma20_gap" not in names
    with pytest.raises(ValidationError, match="没有「"):
        validate("HK", [Condition("ma20_gap", ">=", 0)])


def test_a_stock_without_coverage_is_named_as_missing_not_measured_on_raw_prices(db):
    seed_snapshot(db)
    seed_bars(db)
    # 不写覆盖确认：裸价即使算得出，也不能当作技术面读数
    result = service(db).run("A", [Condition("ma20_gap", ">=", 0.02)], day=DAY)

    assert result.total_hits == 0
    assert any("覆盖" in reason for reason in reasons(result, "ma20_gap"))
    assert result.technical_coverage == (0, 1)


def test_the_no_coverage_group_points_at_the_actions_queue_not_a_terminal(db):
    """选股页那一句「无数」只能是事实（D337）：能补哪种活由 `fix` 说，命令留给命令行。

    「打开该股页面会登记后台补采」是把动作指给了一个页面；选股页自己就有按钮，
    而按钮排的是 `actions`——消费者按全历史重拉并重写覆盖确认。
    """
    seed_snapshot(db)
    seed_bars(db)
    result = service(db).run("A", [Condition("ma20_gap", ">=", 0.02)], day=DAY)
    group = next(group for group in result.missing if group.field == "ma20_gap")
    assert group.fix == "actions"
    assert "覆盖" in group.reason
    assert "补采" not in group.reason and "页面" not in group.reason
    assert "sams " not in group.reason


def test_a_stock_without_bars_asks_for_the_bars_queue_not_a_command(db):
    seed_snapshot(db)
    seed_coverage(db)
    result = service(db).run("A", [Condition("ma20_gap", ">=", 0.02)], day=DAY)
    group = next(group for group in result.missing if group.field == "ma20_gap")
    assert group.fix == "bars"
    assert "日线不足" in group.reason
    assert "sams " not in group.reason


def test_an_unadjustable_series_is_queued_as_actions_not_as_a_command(db):
    seed_snapshot(db)
    days, closes = seed_bars(db)
    # 裸价一天腰斩、又没有记录在案的公司行为：复权序列被拒，能补的活只有重拉公司行为
    closes = [*closes[:-1], closes[-2] * 0.5]
    BarRepo(db).upsert(
        pl.DataFrame(
            {
                "market": ["A"] * len(days),
                "code": [CODE] * len(days),
                "date": days,
                "open": closes,
                "high": [value + 1 for value in closes],
                "low": [value - 1 for value in closes],
                "close": closes,
                "volume": [100.0] * len(days),
                "amount": [value * 100 for value in closes],
                "source": ["akshare"] * len(days),
            }
        )
    )
    seed_coverage(db)
    result = service(db).run("A", [Condition("ma20_gap", ">=", 0.02)], day=DAY)
    group = next(group for group in result.missing if group.field == "ma20_gap")
    assert group.fix == "actions"
    assert "复权序列被拒绝" in group.reason
    assert "sams " not in group.reason


def test_short_windows_still_compute_when_ma250_has_too_few_bars(db):
    seed_snapshot(db)
    seed_bars(db, n=30)
    seed_coverage(db, start=date(2000, 1, 1), end=DAY)
    result = service(db).run(
        "A",
        [Condition("rsi14", ">=", 50), Condition("ma250_gap", ">=", 0)],
        day=DAY,
    )

    assert result.total_hits == 0
    assert any("250" in reason for reason in reasons(result, "ma250_gap"))
    # MA20 有 30 根就够：它不跟着 MA250 一起变成「无数」
    short = service(db).run("A", [Condition("ma20_gap", ">=", 0)], day=DAY)
    assert short.total_hits == 1


def test_volume_ratio_is_missing_when_a_share_change_falls_in_the_window(db):
    seed_snapshot(db)
    days, _ = seed_bars(db)
    seed_coverage(db)
    CorpActionRepo(db).upsert_many(
        pl.DataFrame(
            {
                "market": ["A"],
                "code": [CODE],
                "ex_date": [days[-3]],
                "kind": ["bonus"],
                "cash_per_share": [None],
                "ratio": [1.5],
                "price": [None],
                "src": ["akshare"],
                "note": ["每10股送5股"],
            }
        )
    )
    result = service(db).run(
        "A",
        [Condition("vol_ratio_5d", ">=", 1.0), Condition("ma20_gap", ">=", -1)],
        day=DAY,
    )

    assert result.total_hits == 0
    assert any("量比" in reason for reason in reasons(result, "vol_ratio_5d"))
    group = next(item for item in result.missing if item.field == "vol_ratio_5d")
    assert group.fix == ""  # 送转已经记录在案，再同步同一条源不是下一步
    assert result.technical_coverage == (1, 1)


def test_the_result_reports_how_many_stocks_could_be_computed(db):
    seed_snapshot(db)
    seed_bars(db)
    seed_coverage(db)
    result = service(db).run("A", [Condition("ma20_gap", ">=", -1)], day=DAY)

    assert result.technical_coverage == (1, 1)
    assert "技术面" in result.basis_note
