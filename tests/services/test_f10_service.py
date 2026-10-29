"""F10 服务：写侧如实搬库，读侧四块档案 + 两句空态判据。

写侧钉三件事：概况一票一行（重复执行是覆盖）、0 行不写、链上的降级与跳过要带回调用方。
读侧钉的是「只给展示用」这条边界：港股那几期没有披露日也要能读出来，但它们不因此
变成「已披露」——PR 与选股仍走 `FundamentalService`。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from support import FrozenClock

from sams.core.timeutil import HISTORY_START
from sams.datasource.registry import Attempt, FrameResult
from sams.services.f10 import F10Service, F10SyncService
from sams.storage.repos.corp_actions import CorpAction, CorpActionRepo
from sams.storage.repos.f10 import AnnouncementRepo, CompanyProfileRepo
from sams.storage.repos.financials import FinancialRepo, ValuationRepo

AS_OF = date(2024, 10, 25)
CLOCK = FrozenClock(datetime(2024, 10, 25, tzinfo=UTC))


def profile_frame(market: str = "A", code: str = "600519") -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "market": market,
                "code": code,
                "name": "贵州茅台",
                "industry": "白酒",
                "source": "eastmoney_f10",
            }
        ]
    )


def announcements_frame(n: int = 2) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "market": ["A"] * n,
            "code": ["600519"] * n,
            "art_code": [f"AN{i}" for i in range(n)],
            "published_on": [date(2024, 10, 1) + timedelta(days=i) for i in range(n)],
            "title": [f"公告 {i}" for i in range(n)],
            "category": ["临时公告"] * n,
            "url": [f"https://example.test/{i}" for i in range(n)],
            "source": ["eastmoney_f10"] * n,
        }
    )


def result(method: str, frame: pl.DataFrame, **over) -> FrameResult:
    kind = {"company_profile": "公司概况", "announcements": "公告"}[method]
    defaults = dict(
        market="A",
        code="600519",
        kind=kind,
        frame=frame,
        source="eastmoney_f10",
        attempts=(Attempt("eastmoney_f10", True),),
    )
    return FrameResult(**{**defaults, **over})


class StubRegistry:
    """只回答两条链的结果；降级链本身在 `tests/datasource` 里测。"""

    def __init__(self, *, profile=None, announcements=None, chains=None) -> None:
        self.results = {
            "company_profile": profile
            if profile is not None
            else result("company_profile", profile_frame()),
            "announcements": announcements
            if announcements is not None
            else result("announcements", announcements_frame()),
        }
        self.chains = (
            chains
            if chains is not None
            else {
                "company_profile": ("eastmoney_f10",),
                "announcements": ("eastmoney_f10",),
            }
        )
        self.calls: list[tuple[str, tuple]] = []

    def _fetch(self, method: str, market: str, code: str, start: date, end: date) -> FrameResult:
        self.calls.append((method, (market, code, start, end)))
        outcome = self.results[method]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def fetch_company_profile(self, market, code, start, end):
        return self._fetch("company_profile", market, code, start, end)

    def fetch_announcements(self, market, code, start, end):
        return self._fetch("announcements", market, code, start, end)

    def chain_for(self, market: str, method: str):
        return self.chains.get(method, ())


def make_sync(db, registry=None):
    registry = registry or StubRegistry()
    return (
        F10SyncService(registry, CompanyProfileRepo(db), AnnouncementRepo(db), clock=CLOCK),
        registry,
    )


# --- 写侧：报告的是「带回来了什么」，不是「跑完了没有」 ------------------------------------


def test_profile_sync_writes_one_row_and_rerun_overwrites_it(db):
    service, _ = make_sync(db)
    first = service.sync_profile("A", "600519")
    second = service.sync_profile("A", "600519")
    assert (first.written, second.written) == (1, 1)
    assert CompanyProfileRepo(db).count() == 1


def test_profile_fetch_ignores_the_window(db):
    service, registry = make_sync(db)
    service.sync_profile("A", "600519")
    method, (market, code, start, end) = registry.calls[0]
    assert method == "company_profile"
    assert (market, code) == ("A", "600519")
    assert start == HISTORY_START
    assert end == AS_OF


def test_keys_are_canonical_before_the_chain_and_the_repo(db):
    service, registry = make_sync(db)
    frame = profile_frame("HK", "00700")
    registry.results["company_profile"] = result(
        "company_profile", frame, market="HK", code="00700"
    )
    summary = service.sync_profile("hk", "700")
    assert (summary.market, summary.code) == ("HK", "00700")
    assert CompanyProfileRepo(db).has("HK", "00700")


def test_profile_empty_answer_is_not_written_as_a_row(db):
    empty = result(
        "company_profile",
        profile_frame().head(0),
        source="eastmoney_f10",
        attempts=(Attempt("eastmoney_f10", True, 1, "查无记录"),),
        confirmed_empty_by=("eastmoney_f10",),
    )
    service, _ = make_sync(db, StubRegistry(profile=empty))
    summary = service.sync_profile("A", "600519")
    assert summary.empty is True
    assert summary.written == 0
    assert summary.confirmed_empty_by == ("eastmoney_f10",)
    assert CompanyProfileRepo(db).count() == 0


def test_announcements_rerun_is_stable(db):
    service, _ = make_sync(db)
    first = service.sync_announcements("A", "600519", date(2024, 9, 1), AS_OF)
    second = service.sync_announcements("A", "600519", date(2024, 9, 1), AS_OF)
    assert (first.written, second.written) == (2, 2)
    assert AnnouncementRepo(db).count() == 2


def test_zero_announcements_write_nothing(db):
    empty = result(
        "announcements",
        announcements_frame().head(0),
        attempts=(Attempt("eastmoney_f10", True, 1, "查无记录"),),
        confirmed_empty_by=("eastmoney_f10",),
    )
    service, _ = make_sync(db, StubRegistry(announcements=empty))
    summary = service.sync_announcements("A", "600519", date(2024, 9, 1), AS_OF)
    assert summary.empty is True
    assert summary.written == 0
    assert AnnouncementRepo(db).count() == 0


def test_degraded_and_skipped_are_carried_back(db):
    degraded = result(
        "company_profile",
        profile_frame(),
        attempts=(Attempt("broken", False, 1, "503"), Attempt("eastmoney_f10", True)),
        skipped=(("yfinance_hk", "不提供公司概况"),),
    )
    service, _ = make_sync(db, StubRegistry(profile=degraded))
    summary = service.sync_profile("A", "600519")
    assert summary.degraded is True
    assert summary.skipped == (("yfinance_hk", "不提供公司概况"),)


# --- 读侧：四块档案与两句空态判据 --------------------------------------------------------


def _seed_financials(db, *, count: int = 9) -> date:
    rows = {
        "market": ["HK"] * count,
        "code": ["00700"] * count,
        "report_date": [date(2022, 3, 31) + timedelta(days=90 * i) for i in range(count)],
        "published_date": pl.Series(
            "published_date", [None] * count, dtype=pl.Date
        ),
        "roe": [0.1 + i / 100 for i in range(count)],
        "revenue_yoy": [0.05] * count,
        "profit_yoy": [0.06] * count,
        "dividend_yield": [None] * count,
        "market_cap": [None] * count,
        "source": ["eastmoney_hk_f10"] * count,
    }
    FinancialRepo(db).upsert(pl.DataFrame(rows))
    return max(rows["report_date"])


def _seed_valuation(db) -> None:
    ValuationRepo(db).upsert(
        pl.DataFrame(
            [
                {
                    "market": "HK",
                    "code": "00700",
                    "date": AS_OF - timedelta(days=1),
                    "pe_ttm": 21.5,
                    "pb": 3.4,
                    "dividend_yield": None,
                    "market_cap": 3.6e12,
                    "source": "yfinance",
                }
            ]
        )
    )


def _seed_announcements(db, *, count: int = 12) -> date:
    frame = announcements_frame(count).with_columns(
        pl.lit("HK").alias("market"),
        pl.lit("00700").alias("code"),
    )
    AnnouncementRepo(db).upsert(frame)
    return max(frame["published_on"].to_list())


def _seed_dividends(db, *, count: int = 8) -> None:
    repo = CorpActionRepo(db)
    for i in range(count):
        repo.upsert(
            CorpAction(
                market="HK",
                code="00700",
                ex_date=date(2023, 1, 1) + timedelta(days=90 * i),
                kind="dividend",
                src="test",
                cash_per_share=2.0 + i,
            )
        )


def test_read_model_has_the_four_blocks(db):
    CompanyProfileRepo(db).upsert(
        pl.DataFrame(
            [
                {
                    "market": "HK",
                    "code": "00700",
                    "name": "腾讯控股",
                    "isin": "KYG875721634",
                    "source": "eastmoney_f10",
                }
            ]
        )
    )
    newest_report = _seed_financials(db)
    _seed_valuation(db)
    newest_announcement = _seed_announcements(db)
    _seed_dividends(db)

    read = F10Service(
        StubRegistry(),
        CompanyProfileRepo(db),
        AnnouncementRepo(db),
        FinancialRepo(db),
        ValuationRepo(db),
        CorpActionRepo(db),
        clock=CLOCK,
    ).read("hk", "700")

    assert (read.market, read.code) == ("HK", "00700")
    assert read.profile["name"] == "腾讯控股"
    assert read.profile["isin"] == "KYG875721634"
    # 默认「全部」档摆 6 期、报告期倒序、没有披露日的那几期也在（只给展示，D215）
    assert len(read.financials) == 6
    assert read.period_type == "all" and read.periods == 6
    assert read.financials[0]["report_date"] == newest_report
    assert all(row["published_date"] is None for row in read.financials)
    # 没有披露日不是「预约未披露」：那是 D215 给的展示档案，照常摆数
    assert all(row["scheduled"] is False for row in read.financials)
    # 最近 10 条公告，新在前
    assert len(read.announcements) == 10
    assert read.announcements[0]["published_on"] == newest_announcement
    # 最近 6 次分红送转，新在前
    assert len(read.dividends) == 6
    assert read.dividends[0]["ex_date"] > read.dividends[-1]["ex_date"]
    assert read.valuation["pe_ttm"] == 21.5
    assert read.has_profile_source is True
    assert read.has_announcement_source is True


def test_read_model_does_not_show_future_corp_actions(db):
    repo = CorpActionRepo(db)
    repo.upsert(
        CorpAction(
            market="A",
            code="600519",
            ex_date=AS_OF,
            kind="dividend",
            src="test",
            cash_per_share=19.1,
        )
    )
    repo.upsert(
        CorpAction(
            market="A",
            code="600519",
            ex_date=AS_OF + timedelta(days=10),
            kind="dividend",
            src="test",
            cash_per_share=20.0,
        )
    )
    read = F10Service(
        StubRegistry(),
        CompanyProfileRepo(db),
        AnnouncementRepo(db),
        FinancialRepo(db),
        ValuationRepo(db),
        repo,
        clock=CLOCK,
    ).read("A", "600519")
    assert [row["ex_date"] for row in read.dividends] == [AS_OF]


def test_read_model_reports_a_market_without_f10_sources(db):
    read = F10Service(
        StubRegistry(chains={}),
        CompanyProfileRepo(db),
        AnnouncementRepo(db),
        FinancialRepo(db),
        ValuationRepo(db),
        CorpActionRepo(db),
        clock=CLOCK,
    ).read("US", "AAPL")
    assert read.has_profile_source is False
    assert read.has_financials_source is False
    assert read.has_announcement_source is False
    assert read.profile is None


# ---------- D219：五个报告期档位 ----------

QUARTERS = (
    (date(2026, 6, 30), "中报"),
    (date(2026, 3, 31), "一季报"),
    (date(2025, 12, 31), "年报"),
    (date(2025, 9, 30), "三季报"),
    (date(2025, 6, 30), None),  # 老行没有 report_type：只靠月份兜底
    (date(2025, 3, 31), None),
    (date(2024, 12, 31), None),
    (date(2024, 9, 30), None),
    (date(2024, 6, 30), None),
    (date(2024, 3, 31), None),
    (date(2023, 12, 31), None),
    (date(2023, 9, 30), None),
)


def _seed_quarters(db) -> None:
    rows = []
    for index, (report_date, kind) in enumerate(QUARTERS):
        rows.append(
            {
                "market": "A",
                "code": "600519",
                "report_date": report_date,
                "report_type": kind,
                "roe": 0.1 + index / 100,
                "revenue": 1.0e10 + index,
                "source": "akshare",
            }
        )
    FinancialRepo(db).upsert(pl.DataFrame(rows))


def _read(db, period_type="all"):
    return F10Service(
        StubRegistry(),
        CompanyProfileRepo(db),
        AnnouncementRepo(db),
        FinancialRepo(db),
        ValuationRepo(db),
        CorpActionRepo(db),
        clock=CLOCK,
    ).read("A", "600519", period_type=period_type)


def test_each_period_type_keeps_only_its_kind_and_its_cap(db):
    _seed_quarters(db)

    everything = _read(db)
    assert [row["report_date"] for row in everything.financials] == [
        date(2026, 6, 30),
        date(2026, 3, 31),
        date(2025, 12, 31),
        date(2025, 9, 30),
        date(2025, 6, 30),
        date(2025, 3, 31),
    ]

    annual = _read(db, "annual")
    assert annual.period_type == "annual" and annual.periods == 3
    assert all(row["report_date"].month == 12 for row in annual.financials)
    assert all(row["period_kind"] == "年报" for row in annual.financials)
    assert annual.has_financial_data is True
    assert date(2025, 12, 31) in {row["report_date"] for row in annual.financials}, (
        "老行没有 report_type 时按 12 月兜底，不能整类消失"
    )

    q1 = _read(db, "q1")
    assert q1.periods == 3
    assert all(row["report_date"].month == 3 for row in q1.financials)

    h1 = _read(db, "h1")
    assert h1.periods == 3
    assert all(row["report_date"].month == 6 for row in h1.financials)

    q3 = _read(db, "q3")
    assert q3.periods == 3
    assert all(row["report_date"].month == 9 for row in q3.financials)


def test_the_limit_is_per_period_type(db):
    _seed_quarters(db)
    annual = _read(db, "annual")
    assert len(annual.financials) == annual.periods <= 5
    q1 = _read(db, "q1")
    assert len(q1.financials) == q1.periods <= 8


def test_an_unknown_period_type_is_refused_not_silently_treated_as_all(db):
    from sams.core.errors import ValidationError

    _seed_quarters(db)
    with pytest.raises(ValidationError, match="报告期档位"):
        _read(db, "monthly")


def test_a_report_scheduled_after_today_is_flagged(db):
    """源把预约披露日之后的数也给了：日期要留，但要标成未披露（spec §7）。"""
    FinancialRepo(db).upsert(
        pl.DataFrame(
            {
                "market": ["A", "A"],
                "code": ["600519", "600519"],
                "report_date": [date(2025, 9, 30), date(2025, 6, 30)],
                "published_date": [AS_OF + timedelta(days=1), AS_OF - timedelta(days=30)],
                "roe": [0.2, 0.18],
                "source": ["akshare", "akshare"],
            }
        )
    )
    read = _read(db)
    flags = {row["report_date"]: row["scheduled"] for row in read.financials}
    assert flags == {date(2025, 9, 30): True, date(2025, 6, 30): False}


def test_an_empty_bucket_still_knows_the_local_table_has_rows(db):
    FinancialRepo(db).upsert(
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": "600519",
                    "report_date": date(2025, 12, 31),
                    "report_type": "年报",
                    "roe": 0.3,
                    "source": "akshare",
                }
            ]
        )
    )
    q1 = _read(db, "q1")
    assert q1.financials == ()
    assert q1.has_financial_data is True
