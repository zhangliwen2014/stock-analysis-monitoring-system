"""估值分位条的三件事：位置、历史 20/50/80、以及拒绝时属于哪一档（D238/D242）。

分位不只是「一个 0~100 的数」：屏幕上还要画历史三档刻度，并在拒绝时说出是
「不适用 / 未披露 / 无法计算 / 未覆盖」里的哪一种。这几种拒绝的下一步不同，
所以 `missing_kind` 与 `reason` 一起出门。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import polars as pl
import pytest
from support import FrozenClock

from sams.analysis.valuation.percentile import (
    MIN_SAMPLES,
    DisclosedPeriod,
    ValuationDay,
    metric_percentile,
    pr_percentile,
)
from sams.core.missing import (
    NOT_APPLICABLE,
    NOT_COVERED,
    NOT_DISCLOSED,
    UNCOMPUTABLE,
)
from sams.services.valuation import ValuationService
from sams.storage.repos.financials import FinancialRepo, ValuationRepo

START = date(2025, 3, 2)
ANNUAL = DisclosedPeriod(
    report_date=date(2024, 12, 31),
    roe=0.15,
    published_date=date(2025, 3, 1),
)


def ramp(n: int, *, pe_last: float | None = None) -> list[ValuationDay]:
    """n 天的 PE 从 1 递增到 n；最后一天可换成指定值（默认也是 n）。"""
    rows = [
        ValuationDay(day=START + timedelta(days=i), pe_ttm=float(i + 1), pb=float(i + 1))
        for i in range(n)
    ]
    if pe_last is not None:
        rows[-1] = ValuationDay(day=rows[-1].day, pe_ttm=pe_last, pb=rows[-1].pb)
    return rows


def test_a_full_history_gives_the_rank_and_the_three_quantiles():
    # 今天不投票，所以 251 行才有 250 个样本
    rows = ramp(MIN_SAMPLES + 1)
    reading = pr_percentile(rows, [ANNUAL], rows[-1].day)

    assert reading.ok
    assert reading.samples == MIN_SAMPLES
    assert reading.value == pytest.approx(100.0), "等值算不高于：现在是历史最高"
    # 1..250 的 PR 是 1..250 ÷ (ROE 0.15 × 100)，20/50/80 分位按线性插值
    assert reading.q20 == pytest.approx(50.8 / 15)
    assert reading.q50 == pytest.approx(125.5 / 15)
    assert reading.q80 == pytest.approx(200.2 / 15)
    assert reading.missing_kind == ""


def test_today_does_not_vote_even_when_it_is_an_outlier():
    rows = ramp(MIN_SAMPLES + 1, pe_last=9999.0)
    reading = pr_percentile(rows, [ANNUAL], rows[-1].day)

    assert reading.value == pytest.approx(100.0)
    assert reading.samples == MIN_SAMPLES, "今天那一行不进样本，只在 current 里"


def test_equality_counts_as_at_or_below():
    rows = [
        ValuationDay(day=START + timedelta(days=i), pe_ttm=10.0, pb=10.0)
        for i in range(MIN_SAMPLES + 1)
    ]
    assert pr_percentile(rows, [ANNUAL], rows[-1].day).value == pytest.approx(100.0)


def test_a_short_history_is_uncomputable_not_zero():
    rows = ramp(MIN_SAMPLES)  # 249 个样本：差一天也不出数
    reading = pr_percentile(rows, [ANNUAL], rows[-1].day)

    assert reading.value is None
    assert reading.samples == MIN_SAMPLES - 1
    assert reading.missing_kind == UNCOMPUTABLE
    assert str(MIN_SAMPLES) in reading.reason
    assert reading.q20 is None and reading.q50 is None and reading.q80 is None


def test_no_valuation_rows_is_not_covered():
    reading = pr_percentile([], [ANNUAL], START)
    assert reading.missing_kind == NOT_COVERED
    assert "没有" in reading.reason


def test_a_negative_pe_today_is_not_applicable_for_pr_but_pe_and_pb_still_report():
    # 今天那条 PE 为负不进 PE 样本，所以要多两行才够地板（252 行 → 250 个样本）
    rows = ramp(MIN_SAMPLES + 2, pe_last=-5.0)
    as_of = rows[-1].day

    pr_reading = pr_percentile(rows, [ANNUAL], as_of)
    pe_reading = metric_percentile(rows, "pe_ttm", as_of)
    pb_reading = metric_percentile(rows, "pb", as_of)

    assert pr_reading.missing_kind == NOT_APPLICABLE
    assert pr_reading.current is None
    # PE 那一天没有意义，但前一天的 PE 仍有自己的历史位置；PB 照常出
    assert pe_reading.ok and pe_reading.samples == MIN_SAMPLES
    assert pb_reading.ok


def test_no_disclosed_annual_report_is_not_disclosed():
    rows = ramp(MIN_SAMPLES)
    reading = pr_percentile(rows, [], rows[-1].day)

    assert reading.value is None
    assert reading.current is None
    assert reading.missing_kind == NOT_DISCLOSED
    assert "ROE" in reading.reason


def test_undisclosed_periods_are_counted_even_when_a_report_is_found():
    periods = [
        ANNUAL,
        DisclosedPeriod(report_date=date(2023, 12, 31), roe=12.0, published_date=None),
    ]
    rows = ramp(MIN_SAMPLES + 1)
    reading = pr_percentile(rows, periods, rows[-1].day)
    assert reading.ok
    assert reading.unpublished_periods == 1
    assert "没有发布日" in reading.evidence


def test_metric_percentile_with_only_bad_values_is_uncomputable():
    rows = [
        ValuationDay(day=START + timedelta(days=i), pe_ttm=-1.0, pb=None)
        for i in range(10)
    ]
    reading = metric_percentile(rows, "pb", rows[-1].day)
    # 全是缺失（pb 全 None）→ 未覆盖；有行但没有一个可用正数 → 无法计算
    assert reading.missing_kind == NOT_COVERED

    mixed = [
        ValuationDay(day=START + timedelta(days=i), pe_ttm=-1.0, pb=float(i + 1))
        for i in range(MIN_SAMPLES + 1)
    ]
    assert metric_percentile(mixed, "pb", mixed[-1].day).ok
    assert metric_percentile(mixed, "pe_ttm", mixed[-1].day).missing_kind == UNCOMPUTABLE


def test_percentiles_service_returns_pr_pe_pb_and_a_coverage_note(db):
    as_of = START + timedelta(days=MIN_SAMPLES)
    _seed(db, ramp(MIN_SAMPLES + 1))
    service = ValuationService(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        FinancialRepo(db),
        ValuationRepo(db),
        None,  # type: ignore[arg-type]
        clock=FrozenClock(datetime(2026, 1, 1, 15)),
    )

    report = service.percentiles("A", "600519", as_of=as_of)

    assert [row.metric for row in report.rows] == ["pr", "pe_ttm", "pb"]
    assert all(row.ok for row in report.rows)
    assert report.window_years == 5
    assert "本地估值序列从" in report.coverage_note
    assert str(as_of) in report.coverage_note or str(MIN_SAMPLES) in report.coverage_note


def test_a_sequence_covering_the_whole_window_says_nothing_about_coverage(db):
    as_of = date(2026, 10, 30)
    days = [as_of - timedelta(days=7 * i) for i in range(320)][::-1]
    _seed(db, [ValuationDay(day=day, pe_ttm=10.0, pb=2.0) for day in days])
    service = ValuationService(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        FinancialRepo(db),
        ValuationRepo(db),
        None,  # type: ignore[arg-type]
        clock=FrozenClock(datetime(2026, 10, 30, 15)),
    )

    report = service.percentiles("A", "600519", as_of=as_of)

    assert report.coverage_note == ""
    assert report.rows[0].ok


def test_hk_without_valuation_rows_is_not_covered_instead_of_raising(db):
    service = ValuationService(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        FinancialRepo(db),
        ValuationRepo(db),
        None,  # type: ignore[arg-type]
        clock=FrozenClock(datetime(2026, 1, 1, 15)),
    )
    report = service.percentiles("HK", "00700", as_of=date(2026, 1, 1))
    assert all(row.missing_kind == NOT_COVERED for row in report.rows)


def _seed(db, rows: list[ValuationDay]) -> None:
    ValuationRepo(db).upsert(
        pl.DataFrame(
            {
                "market": ["A"] * len(rows),
                "code": ["600519"] * len(rows),
                "date": [row.day for row in rows],
                "pe_ttm": [row.pe_ttm for row in rows],
                "pb": [row.pb for row in rows],
                "source": ["test"] * len(rows),
            }
        )
    )
    # 五年窗口里每年一份年报，PIT 才在每个交易日都配得上分母
    annuals = [
        {
            "market": "A",
            "code": "600519",
            "report_date": date(year, 12, 31),
            "published_date": date(year + 1, 3, 1),
            "roe": ANNUAL.roe,
            "source": "test",
        }
        for year in range(2020, 2025)
    ]
    FinancialRepo(db).upsert(pl.DataFrame(annuals))
