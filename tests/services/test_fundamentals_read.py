"""读取用例：把「今天的 PE」与「最近已披露那期的 ROE」配成一对，并把配对的条件说出口。

PR 的算式在 `analysis.fundamentals`（纯函数），这一层只决定**喂进去的是哪两个数**。
四个会静默出错的地点：

1. **未来函数**（D61）。2024Q1 的报表 4 月 26 日才公开：`as_of` 早于披露日时，
   那一期的 ROE 连同由它算出的 PR 必须一起消失，而不是「先取回来备用」。
2. **披露日为 NULL**。那些行不参与采信，但也不能当作不存在——`dropped_unpublished`
   必须带回来，屏幕上要有一句「另有 N 期因缺披露日未被采信」。
3. **两个粒度**（D60）。报告期与估值日各是各的：`period` 与 `valuation_on` 分列，
   排版不许让人以为它们是同一天的数。
4. **as_of 不许是 None**。一路传到 SQL 会把「没有截至日」变成「库里所有期都算已披露」，
   缺省值只能是今天，而且由注入的时钟给（测试里冻结，永不等真实时间）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest
import support

from sams.analysis.fundamentals import BASIS_PB_ESTIMATE, BASIS_PE, BASIS_REFUSED
from sams.services.fundamentals import FundamentalService
from sams.storage.repos.financials import FinancialRepo, ValuationRepo

Q4 = date(2023, 12, 31)
Q1 = date(2024, 3, 31)
MID = date(2024, 6, 30)
Q4_PUBLISHED = date(2024, 3, 20)
Q1_PUBLISHED = date(2024, 4, 26)
VALUATION_DAY = date(2024, 6, 28)
TODAY = date(2024, 7, 9)

#: 与仓库层同一套类型：测试自己造的帧必须与采集出口进来的帧同形
TEXT = pl.Utf8
FINANCIAL_SCHEMA = {
    "market": TEXT,
    "code": TEXT,
    "report_date": pl.Date,
    "published_date": pl.Date,
    "roe": pl.Float64,
    "revenue_yoy": pl.Float64,
    "profit_yoy": pl.Float64,
    "dividend_yield": pl.Float64,
    "market_cap": pl.Float64,
    "source": TEXT,
}
VALUATION_SCHEMA = {
    "market": TEXT,
    "code": TEXT,
    "date": pl.Date,
    "pe_ttm": pl.Float64,
    "pb": pl.Float64,
    "dividend_yield": pl.Float64,
    "market_cap": pl.Float64,
    "source": TEXT,
}


def service(db, *, today: date = TODAY) -> FundamentalService:
    return FundamentalService(
        FinancialRepo(db),
        ValuationRepo(db),
        clock=support.FrozenClock(datetime(today.year, today.month, today.day, 15, tzinfo=UTC)),
    )


def seed_financials(db, *rows: dict) -> None:
    """默认 2023Q4 一期（ROE 24.11%，3 月 20 日公开），用例按行覆盖或追加。"""
    defaults = {
        "market": "A",
        "code": "601088",
        "report_date": Q4,
        "published_date": Q4_PUBLISHED,
        "roe": 0.2411,
        "revenue_yoy": 0.0,
        "profit_yoy": None,
        "dividend_yield": 0.051,
        "market_cap": 6.4e11,
        "source": "akshare",
    }
    FinancialRepo(db).upsert(
        pl.DataFrame([{**defaults, **row} for row in rows], schema=FINANCIAL_SCHEMA)
    )


def seed_valuation(db, *rows: dict) -> None:
    defaults = {
        "market": "A",
        "code": "601088",
        "date": VALUATION_DAY,
        "pe_ttm": 20.0,
        "pb": 2.1,
        "dividend_yield": None,
        "market_cap": 6.6e11,
        "source": "akshare",
    }
    ValuationRepo(db).upsert(
        pl.DataFrame([{**defaults, **row} for row in rows], schema=VALUATION_SCHEMA)
    )


def test_the_unit_self_check_pe_20_with_roe_20_percent_is_one(db):
    """`PE=20 / ROE=20% ⇒ PR≈1.00`——单位口径错了会得 0.01，而屏幕上两者都像「便宜」。"""
    seed_financials(db, {"report_date": Q1, "roe": 0.2, "published_date": Q1_PUBLISHED})
    seed_valuation(db, {})
    read = service(db).read("A", "601088", as_of=TODAY)
    assert read.pr.value == pytest.approx(1.0, abs=1e-9)
    assert read.pr.basis == BASIS_PE
    assert read.pr.roe_used == pytest.approx(0.2)


def test_the_two_grains_are_reported_separately(db):
    seed_financials(db, {"report_date": Q1, "published_date": Q1_PUBLISHED})
    seed_valuation(db, {})
    read = service(db).read("A", "601088", as_of=TODAY)
    assert (read.period, read.published_on) == (Q1, Q1_PUBLISHED)
    assert read.valuation_on == VALUATION_DAY, "PE 是哪一天的，得和报告期分开说"
    assert (read.source, read.valuation_source) == ("akshare", "akshare")


def test_an_undisclosed_period_is_invisible_until_its_publication_date(db):
    """反未来函数专项：`as_of` 早于公开日时，那一期的 ROE 与由它算出的 PR 一起消失。"""
    seed_financials(
        db,
        {"roe": 0.2411},  # 2023Q4，3 月 20 日已公开
        {"report_date": Q1, "roe": 0.2, "published_date": Q1_PUBLISHED},
    )
    seed_valuation(db, {"date": date(2024, 4, 24)}, {})
    before = service(db).read("A", "601088", as_of=date(2024, 4, 25))
    assert before.period == Q4, "只该采信 3 月 20 日已公开的那一期"
    assert before.valuation_on == date(2024, 4, 24), "PE 也只能取截至日之前的那一行"
    assert before.pr.roe_used == pytest.approx(0.2411)
    after = service(db).read("A", "601088", as_of=date(2024, 4, 26))
    assert after.period == Q1 and after.pr.roe_used == pytest.approx(0.2)


def test_a_period_without_a_publication_date_is_excluded_and_counted(db):
    """NULL 披露日不能当「早就披露了」：它是「不知道」，而不知道的那一期会替未来背书。"""
    seed_financials(
        db,
        {"roe": 0.2411},
        {"report_date": Q1, "roe": 0.2, "published_date": Q1_PUBLISHED},
        # 最新的一期恰恰是最不知道何时公开的一期：它必须被挡在采信之外
        {"report_date": MID, "published_date": None, "roe": 0.9},
    )
    seed_valuation(db, {})
    read = service(db).read("A", "601088", as_of=TODAY)
    assert read.period == Q1, "没有披露日的那一期不参与采信"
    assert read.dropped_unpublished == 1
    assert read.pr.roe_used == pytest.approx(0.2), "PR 用的是被采信的那一期"


def test_a_negative_pe_takes_the_pb_branch_with_an_estimated_roe(db):
    seed_financials(db, {"report_date": Q1, "roe": 0.2, "published_date": Q1_PUBLISHED})
    seed_valuation(db, {"pe_ttm": -8.0})
    read = service(db).read("A", "601088", as_of=TODAY, roe_estimate=0.2)
    assert read.pr.basis == BASIS_PB_ESTIMATE
    # PB=2.1、预估 ROE=0.2 ⇒ 2.1 / 0.2 / 0.2 / 100 = 0.525
    assert read.pr.value == pytest.approx(0.525)
    assert read.pr.estimated is True


def test_a_negative_pe_without_an_estimate_refuses_and_names_the_next_step(db):
    seed_financials(db, {"report_date": Q1, "roe": -0.1, "published_date": Q1_PUBLISHED})
    seed_valuation(db, {"pe_ttm": -8.0})
    read = service(db).read("A", "601088", as_of=TODAY)
    assert read.pr.value is None and read.pr.basis == BASIS_REFUSED
    assert "roe-estimate" in read.pr.reason, "拒绝要说清下一步，而不是给个 0 让人误读"


def test_a_symbol_with_no_fundamentals_at_all_says_so_instead_of_zero(db):
    """库里一行都没有时，PR 不是 0 也不是「PE 缺失」——那句假话会让人去换源。"""
    read = service(db).read("A", "999999", as_of=TODAY)
    assert read.period is None and read.valuation_on is None
    assert read.pr.value is None and read.pr.basis == BASIS_REFUSED
    assert "sams sync" in read.pr.reason


def test_the_default_as_of_is_the_clock_not_an_empty_string_in_sql(db):
    """缺省截至日 = 今天（由注入的时钟给）。None 传到 SQL 会把「没有截至日」读成「全部已披露」。"""
    seed_financials(db, {"report_date": Q1, "published_date": Q1_PUBLISHED})
    seed_valuation(db, {"date": TODAY})
    read = service(db).read("A", "601088")
    assert read.as_of == TODAY
    assert read.valuation_on == TODAY


def test_the_market_and_key_spellings_do_not_change_the_answer(db):
    seed_financials(
        db,
        {
            "market": "HK",
            "code": "00700",
            "report_date": Q1,
            "published_date": Q1_PUBLISHED,
        },
    )
    seed_valuation(db, {"market": "HK", "code": "00700"})
    read = service(db).read("hk", "700", as_of=TODAY)
    assert (read.market, read.code) == ("HK", "00700")
    assert read.period == Q1 and read.pr.value is not None


def test_the_market_cap_prefers_the_daily_row_over_the_report_period_one(db):
    """市值是某一天的数（D60）：报告期的那一列只在没有按日估值行时兜底。"""
    seed_financials(
        db,
        {"report_date": Q1, "published_date": Q1_PUBLISHED, "market_cap": 6.4e11},
        {"code": "600000", "report_date": Q1, "published_date": Q1_PUBLISHED, "market_cap": 1.0e11},
    )
    seed_valuation(db, {"market_cap": 6.6e11})
    assert service(db).read("A", "601088", as_of=TODAY).market_cap == 6.6e11
    # 只有报告期数据的票：市值仍然出得来，但要说明它是**哪一期**的数
    alone = service(db).read("A", "600000", as_of=TODAY)
    assert alone.market_cap == 1.0e11 and alone.valuation_on is None
