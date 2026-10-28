"""选股引擎：每条件三个数、三者相加等于票池、无数出局的票带原因点名（D180）。"""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest
from support_valuation import financial_rows

from sams.core.errors import ValidationError
from sams.core.screener_fields import Condition
from sams.services.screener import ScreenerService
from sams.storage.repos.financials import FinancialRepo
from sams.storage.repos.snapshots import SnapshotRepo
from sams.storage.repos.symbol_list import SymbolListRepo

DAY = date(2026, 10, 28)
TAKEN = datetime(2026, 10, 28, 8, 41, tzinfo=UTC)


def snap(code, *, pe_dyn=10.0, pb=1.0, turnover=1.0, price=10.0, quoted=DAY):
    return {
        "code": code,
        "name": f"票{code}",
        "quoted_on": quoted,
        "price": price,
        "pct_chg": 1.0,
        "amount": 1e8,
        "turnover_rate": turnover,
        "volume_ratio": 1.0,
        "amplitude": 1.0,
        "pe_ttm": pe_dyn,
        "pe_dyn": pe_dyn,
        "pb": pb,
        "market_cap": 100.0,
        "float_cap": 90.0,
        "is_st": False,
        "source": "tencent",
    }


def seed(db, *rows, codes=("600519",)):
    from sams.datasource.spot import SPOT_SCHEMA

    SymbolListRepo(db).upsert_batch(
        "A",
        [
            {"code": c, "name": c, "py_initials": c, "py_full": c, "currency": "CNY"}
            for c in codes
        ],
    )
    data = [{"market": "A", "taken_at": TAKEN, **row} for row in rows]
    frame = pl.DataFrame(
        data,
        schema={
            "market": pl.Utf8,
            "taken_at": pl.Datetime("us", "UTC"),
            **SPOT_SCHEMA,
        },
    )
    SnapshotRepo(db).replace_day(market="A", day=DAY, taken_at=TAKEN, frame=frame)


def service(db):
    return ScreenerService(SnapshotRepo(db), SymbolListRepo(db), FinancialRepo(db))


def test_every_condition_reports_passed_failed_missing_that_sum_to_the_universe(db):
    seed(
        db,
        snap("600519", pe_dyn=5.0, pb=1.0),   # PR = 25/100 = 0.25 通过
        snap("000001", pe_dyn=6.0, pb=1.0),   # PR = 36/100 = 0.36 通过
        snap("300750", pe_dyn=10.0, pb=1.0),  # PR = 100/100 = 1.00 不通过
        snap("600000", pe_dyn=-5.0, pb=1.0),  # 亏损 → 无数
        codes=("600519", "000001", "300750", "600000"),
    )
    result = service(db).run("A", [Condition("pr_now", "<=", 0.5)])
    counts = result.fields[0]
    assert (counts.passed, counts.failed, counts.missing) == (2, 1, 1)
    assert counts.passed + counts.failed + counts.missing == result.universe_size == 4
    assert [hit.code for hit in result.hits] == ["600519", "000001"]


def test_hits_carry_the_values_of_the_condition_that_picked_them(db):
    seed(db, snap("600519", pe_dyn=5.0, pb=1.0))
    hit = service(db).run("A", [Condition("pr_now", "<=", 0.5)]).hits[0]
    assert hit.values["pr_now"] == pytest.approx(0.25)
    assert hit.values["price"] == 10.0


def test_nothing_missing_means_no_missing_groups(db):
    seed(db, snap("600519"))
    result = service(db).run("A", [Condition("price", ">=", 1.0)])
    assert result.missing == () and result.fields[0].missing == 0


def test_missing_codes_are_grouped_with_a_reason_and_a_next_step(db):
    seed(db, snap("600519", pe_dyn=-5.0, pb=1.0))
    result = service(db).run("A", [Condition("pr_now", "<=", 0.5)])
    (group,) = result.missing
    assert group.field == "pr_now" and group.codes == ("600519",)
    assert "亏损" in group.reason


def test_financial_fields_join_as_of_the_snapshot_day_and_report_the_gap(db):
    seed(db, snap("600519"), snap("000001"), codes=("600519", "000001"))
    # support_valuation.financial_rows 的签名是 (*, market="A", code=CODE)，FY23 的 ROE=0.20
    FinancialRepo(db).upsert(financial_rows(code="600519"))
    result = service(db).run("A", [Condition("roe", ">=", 0.1)])
    assert result.fields[0].passed == 1
    (group,) = result.missing
    assert group.codes == ("000001",) and "financials" in group.reason


def test_snapshot_only_and_list_only_are_two_separate_numbers(db):
    """D174：快照里多出来的、代码表里缺的，两个数分开报，不合并成一个「有效票池」。"""
    seed(db, snap("600519"), snap("999999"), codes=("600519", "000001"))
    result = service(db).run("A", [Condition("price", ">=", 1.0)])
    assert result.snapshot_only == 1  # 999999 不在代码表
    assert result.list_only == 1      # 000001 没有快照
    assert result.universe_size == 1


def test_no_snapshot_returns_a_named_empty_result_not_an_exception(db):
    result = service(db).run("A", [Condition("price", ">=", 1.0)])
    assert result.universe_size == 0 and result.day is None
    assert "sams sync spot" in result.note


def test_limit_truncates_but_the_total_stays_visible(db):
    seed(
        db,
        *(snap(c) for c in ("600519", "000001", "300750")),
        codes=("600519", "000001", "300750"),
    )
    result = service(db).run("A", [Condition("price", ">=", 1.0)], limit=2)
    assert len(result.hits) == 2 and result.total_hits == 3 and result.truncated == 1


def test_validation_happens_before_the_database_is_read(db):
    """词表不认识的字段在碰库之前就拒：否则拼错的字段会被读成「没有票通过」。"""
    with pytest.raises(ValidationError):
        service(db).run("A", [Condition("unknown", "<=", 0.5)])
