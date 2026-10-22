"""五个数据 job 的两条规矩：窗口从库里已有的日子往前，失败按只隔离。

窗口断言看的是**问出去的区间**（`adapter.calls`），不是写回来的行数：一个把 730 天
写死的 job 也能把今天的 K 线放进库，只是每晚都把所有源重拉一遍。
按只隔离看的是**其余那一只**：一只票的链断了，剩下那只照常入库，断的那只进 `errors`。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from support import FrozenClock, sample_bars
from support_fundamentals import StubFundamentalsAdapter
from support_valuation import financial_rows, valuation

from sams.core.errors import RateLimited
from sams.datasource.registry import AdapterRegistry
from sams.services.corp_actions import CorpActionSyncService
from sams.services.fundamentals import FundamentalSyncService
from sams.services.scheduler.jobs import (
    build_jobs,
    run_actions,
    run_bars,
    run_financials,
    run_valuation,
)
from sams.services.scheduler.types import JobContext, JobDeps
from sams.services.scheduler.universe import Universe
from sams.services.sync import SyncService
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.corp_actions_coverage import CorpActionsCoverageRepo
from sams.storage.repos.financials import FinancialRepo, FundingFlowRepo, ValuationRepo
from sams.storage.repos.symbols import SymbolRepo

AS_OF = date(2024, 10, 25)


class RecordingBarsAdapter:
    """只有日线能力的 akshare 替身：记下每一次问出去的区间。

    `markets` 含 HK——注册表在构造时就校验「路由里的源支持该市场」，
    而注册用例要把 akshare 同时排进 A 与 HK 的链上。
    """

    name = "akshare"
    markets = frozenset({"A", "HK"})

    def __init__(self, *, frame: pl.DataFrame | None = None, fail_on: str | None = None) -> None:
        self.frame = frame
        self.fail_on = fail_on
        self.calls: list[tuple[str, str, date, date]] = []

    def daily_bars(self, market, code, start, end):
        self.calls.append((market, code, start, end))
        if code == self.fail_on:
            raise RateLimited(self.name, "429")
        if self.frame is None:
            return pl.DataFrame()
        return self.frame.filter(
            (pl.col("market") == market)
            & (pl.col("code") == code)
            & pl.col("date").is_between(start, end)
        )


def deps(db, settings, *, bars=None, finance=None):
    """装配这条链上真正跑到的那几个服务；汇率/日历/规则那几个槽位这里是 None，本文件不跑它们。"""
    bars = bars or RecordingBarsAdapter()
    finance = finance or StubFundamentalsAdapter(["A"])
    finance.name = "eastmoney"
    registry = AdapterRegistry(
        adapters={"akshare": bars, "eastmoney": finance},
        market_sources={"A": ("akshare", "eastmoney"), "HK": ("akshare",)},
        sleep=lambda _: None,
    )
    symbols = SymbolRepo(db)
    return JobDeps(
        settings=settings,
        registry=registry,
        universe=None,  # type: ignore[arg-type]  —— 本文件的用例自己造 Universe
        sync=SyncService(registry, BarRepo(db), symbols, settings.markets),
        corp_actions=CorpActionSyncService(
            registry, CorpActionRepo(db), CorpActionsCoverageRepo(db)
        ),
        fundamentals=FundamentalSyncService(
            registry,
            FinancialRepo(db),
            ValuationRepo(db),
            FundingFlowRepo(db),
            symbols,
        ),
        fx=None,  # type: ignore[arg-type]
        calendar=None,  # type: ignore[arg-type]
        rules=None,  # type: ignore[arg-type]
        recheck=None,  # type: ignore[arg-type]
        users=None,  # type: ignore[arg-type]
        clock=FrozenClock(datetime(2024, 10, 25, 15, tzinfo=UTC)),
    )


def ctx(*codes: str) -> JobContext:
    return JobContext(
        period_key="2024-10-25",
        as_of=AS_OF,
        market="A",
        universe=Universe(by_market={"A": codes}, fx_pairs=()),
    )


def test_bars_job_is_incremental(db, settings):
    BarRepo(db).upsert(
        sample_bars(start=date(2024, 10, 24)).filter(pl.col("date") == date(2024, 10, 24))
    )
    adapter = RecordingBarsAdapter(
        frame=sample_bars(start=date(2024, 10, 25)).filter(pl.col("date") == date(2024, 10, 25))
    )
    outcome = run_bars(deps(db, settings, bars=adapter), ctx("600519"))
    assert adapter.calls == [("A", "600519", date(2024, 10, 25), AS_OF)]
    assert outcome.rows_affected == 1
    assert BarRepo(db).latest_date("A", "600519") == AS_OF


def test_bars_job_backfills_when_the_code_is_new(db, settings):
    adapter = RecordingBarsAdapter(frame=sample_bars())
    run_bars(deps(db, settings, bars=adapter), ctx("600519"))
    _, _, start, end = adapter.calls[0]
    assert end == AS_OF and start == AS_OF - timedelta(days=730)


def test_one_bad_code_does_not_drop_the_rest(db, settings):
    frame = pl.concat([sample_bars(code="600519"), sample_bars(code="601088")]).filter(
        pl.col("date") == date(2024, 1, 2)
    )
    adapter = RecordingBarsAdapter(frame=frame, fail_on="600519")
    outcome = run_bars(deps(db, settings, bars=adapter), ctx("600519", "601088"))
    assert len(outcome.errors) == 1 and "600519" in outcome.errors[0]
    assert outcome.rows_affected == 1
    assert "1/2" in outcome.note


def test_empty_universe_is_a_successful_noop(db, settings):
    outcome = run_bars(deps(db, settings), ctx())
    assert outcome.errors == () and outcome.rows_affected == 0


def test_a_programming_error_is_not_collected_as_a_failed_code(db, settings, monkeypatch):
    """`_collect` 只接本项目的失败：一个编程错误必须原样冒出去，而不是变成一条 `errors`。

    被接住的编程错误出门时说的是「这只票取不到」，于是要修的那行代码没人会去修。
    """

    def boom(self, market, code):
        raise TypeError("latest_date 收到了没有键的一行")

    monkeypatch.setattr(BarRepo, "latest_date", boom)
    with pytest.raises(TypeError):
        run_bars(deps(db, settings), ctx("600519", "601088"))


def test_financials_job_writes_what_the_chain_answers(db, settings):
    finance = StubFundamentalsAdapter(["A"], financials=financial_rows())
    outcome = run_financials(deps(db, settings, finance=finance), ctx("601088"))
    assert outcome.errors == ()
    assert outcome.rows_affected == 2
    assert db.row_count("financials") == 2
    assert finance.calls == ["financials"]


def test_valuation_job_uses_the_latest_stored_day(db, settings):
    ValuationRepo(db).upsert(valuation([date(2024, 10, 24)]))
    finance = StubFundamentalsAdapter(["A"], valuation=valuation([date(2024, 10, 24), AS_OF]))
    outcome = run_valuation(deps(db, settings, finance=finance), ctx("601088"))
    assert outcome.rows_affected == 2
    assert ValuationRepo(db).latest_date("A", "601088") == AS_OF


def test_actions_job_skips_a_fully_covered_code(db, settings):
    CorpActionsCoverageRepo(db).record(
        "A",
        "601088",
        covered_from=date(1990, 1, 1),
        covered_to=AS_OF,
        full_history=True,
        src="akshare",
    )
    adapter = RecordingBarsAdapter()
    outcome = run_actions(deps(db, settings, bars=adapter), ctx("601088"))
    # 没被问过才算「跳过」：这条链上没有任何源能给公司行为，真去取就会留下一条 errors
    assert outcome.errors == ()
    assert outcome.rows_affected == 0
    assert db.row_count("corp_actions") == 0


def test_build_jobs_registers_only_markets_with_capability(db, settings):
    only = replace(
        settings, markets={k: v for k, v in settings.markets.items() if k in ("A", "HK")}
    )
    jobs = build_jobs(deps(db, only))
    registered = {(job.name, job.market) for job in jobs}
    assert ("bars", "A") in registered and ("bars", "HK") in registered
    assert ("financials", "A") in registered
    assert ("financials", "HK") not in registered
    assert not any(job.name == "actions" for job in jobs)


def test_build_jobs_skips_a_market_without_any_source(db, settings):
    """库里一个启用源都没有的市场：注册表这里不许抛。

    否则「某个市场暂时没源」会让整个常驻调度器起不来，连其余两个市场也不跑。
    """
    built = deps(db, settings)
    built.registry.market_sources = {"A": ("akshare", "eastmoney")}
    jobs = build_jobs(built)
    assert ("bars", "A") in {(job.name, job.market) for job in jobs}
