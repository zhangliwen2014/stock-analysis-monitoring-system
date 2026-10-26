"""五个数据 job 的两条规矩：窗口从库里已有的日子往前，失败按只隔离。

窗口断言看的是**问出去的区间**（`adapter.calls`），不是写回来的行数：一个把 730 天
写死的 job 也能把今天的 K 线放进库，只是每晚都把所有源重拉一遍。
按只隔离看的是**其余那一只**：一只票的链断了，剩下那只照常入库，断的那只进 `errors`。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from support import sample_actions, sample_bars
from support_fundamentals import StubFundamentalsAdapter
from support_valuation import financial_rows, flow_rows, valuation

from sams.core.config import IndicesCfg
from sams.core.errors import RateLimited
from sams.core.version import VersionGate
from sams.datasource.registry import AdapterRegistry
from sams.services.corp_actions import CorpActionSyncService
from sams.services.fundamentals import FundamentalSyncService
from sams.services.scheduler.jobs import (
    build_jobs,
    run_actions,
    run_bars,
    run_financials,
    run_flow,
    run_indices,
    run_valuation,
)
from sams.services.scheduler.types import JobContext, JobDeps
from sams.services.scheduler.universe import Universe
from sams.services.sync import SyncService
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.corp_actions_coverage import CorpActionsCoverageRepo
from sams.storage.repos.financials import FinancialRepo, FundingFlowRepo, ValuationRepo
from sams.storage.repos.index_daily import IndexDailyRepo
from sams.storage.repos.symbols import SymbolRepo

AS_OF = date(2024, 10, 25)


class RecordingBarsAdapter:
    """日线 + 公司行为的记录替身：记下每一次问出去的区间。

    `name`/`markets` 按实例传入，以对齐真实源：`akshare` 只覆盖 A 股（`akshare_cn.py`），
    港股日线另由 `yfinance` 供（`yfinance_us.py`）——注册表在构造时就校验「路由里的源
    支持该市场」，把一个只吃 A 的名字排进 HK 的链上是这里要避开的谎。两个名字都只提供
    真实源确实提供的那几种能力（日线、公司行为）。
    """

    def __init__(
        self,
        *,
        name: str = "akshare",
        markets: tuple[str, ...] = ("A",),
        frame: pl.DataFrame | None = None,
        fail_on: str | None = None,
    ) -> None:
        self.name = name
        self.markets = frozenset(markets)
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

    def corp_actions(self, market, code, start, end):
        return sample_actions(market=market, code=code, source=self.name)


class ListingBarsAdapter(RecordingBarsAdapter):
    """akshare 的真实清单里还有代码表（`akshare_cn.symbol_list`）：这一枚就是它。

    注册表按「方法名在不在」判能力，所以代码表那一道的正例只要方法存在就够了，
    答回来的那两行没人读——`build_jobs` 只问链上有没有谁。
    """

    def symbol_list(self, market):
        return pl.DataFrame({"code": ["000001"], "name": ["平安银行"]})


class StubIndexAdapter:
    """指数日线的记录替身：与真实的 `tencent.index_daily` 同签名，只吃 A 市。

    只回被问到的最后一天：源给宽区间是常态，`_fetch_bars_frame` 那一头会裁掉之外日子，
    这里裁好才不会让「写了几行」替窗口说话。
    """

    name = "tencent"
    markets = ("A",)

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, date, date]] = []

    def index_daily(self, market, code, start, end):
        self.calls.append((market, code, start, end))
        return sample_bars(market=market, code=code, source=self.name, start=end).filter(
            pl.col("date") == end
        )


def deps(
    db,
    settings,
    *,
    bars=None,
    finance=None,
    hk=None,
    index=None,
    with_indices=True,
):
    """装配这条链上真正跑到的那几个服务；汇率/日历/规则那几个槽位这里是 None，本文件不跑它们。

    `index` 给 A 市那条链添一个指数日线的源，`with_indices=False` 则让 `SyncService`
    拿不到指数仓库——大盘条那两道闸（能力与注入）要分开钉。
    """
    bars = bars or RecordingBarsAdapter()
    hk = hk or RecordingBarsAdapter(name="yfinance", markets=("HK",))
    finance = finance or StubFundamentalsAdapter(["A"])
    finance.name = "eastmoney"
    adapters = {"akshare": bars, "yfinance": hk, "eastmoney": finance}
    market_sources: dict[str, tuple[str, ...]] = {
        "A": ("akshare", "eastmoney"),
        "HK": ("yfinance",),
    }
    if index is not None:
        adapters[index.name] = index
        market_sources["A"] = (*market_sources["A"], index.name)
    registry = AdapterRegistry(
        adapters=adapters, market_sources=market_sources, sleep=lambda _: None
    )
    symbols = SymbolRepo(db)
    return JobDeps(
        settings=settings,
        registry=registry,
        sync=SyncService(
            registry,
            BarRepo(db),
            symbols,
            settings.markets,
            indices=IndexDailyRepo(db) if with_indices else None,
        ),
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


def test_bars_job_bumps_the_version_gate(db, settings):
    """写回来的日线必须让盘中缓存那一侧知道（§5.4）：`rows_affected` 就是那句「写了几行」。

    这里不测缓存（缓存在 `test_rules_cache.py`），只测这一头有没有把闸敲响：
    漏掉这一行的后果不是报错，是盘中线程拿着昨天的复权价继续判今天的规则。
    """
    gate = VersionGate()
    BarRepo(db).upsert(
        sample_bars(start=date(2024, 10, 24)).filter(pl.col("date") == date(2024, 10, 24))
    )
    adapter = RecordingBarsAdapter(
        frame=sample_bars(start=date(2024, 10, 25)).filter(pl.col("date") == date(2024, 10, 25))
    )
    run_bars(
        replace(deps(db, settings, bars=adapter), versions=gate), ctx("600519")
    )
    assert gate.current("bars") == 1


def test_a_noop_bars_job_leaves_the_gate_alone(db, settings):
    """一只票都没写就不 bump：清空整张缓存换来的是一次全市场重读，而库里什么都没变。"""
    gate = VersionGate()
    outcome = run_bars(replace(deps(db, settings), versions=gate), ctx())
    assert outcome.rows_affected == 0 and gate.current("bars") == 0


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


def test_flow_job_uses_the_latest_stored_day(db, settings):
    # 库里资金流停在 24 号 ⇒ 窗口从 25 号起（读的是 flow.latest_date）；桩无视传入区间，
    # 所以把按日估值那一列也铺到 AS_OF：若 run_flow 误读 valuation.latest_date，窗口会算成
    # 「已追平」而写 0 行——rows_affected 与 flow 最新日两处都会红。
    FundingFlowRepo(db).upsert(flow_rows([date(2024, 10, 24)], net=1.0e8))
    ValuationRepo(db).upsert(valuation([AS_OF]))
    finance = StubFundamentalsAdapter(
        ["A"], flow=flow_rows([date(2024, 10, 24), AS_OF], net=1.0e8)
    )
    outcome = run_flow(deps(db, settings, finance=finance), ctx("601088"))
    assert outcome.rows_affected == 2
    assert FundingFlowRepo(db).latest_date("A", "601088") == AS_OF


def test_financials_job_bumps_the_gate_because_the_snapshot_reads_it(db, settings):
    """财报进的是盘中快照里 ROE 那一项（`fundamentals.read`）：写回来就得让缓存知道。

    漏掉这一下的症状同日线那一枚：不是报错，是盘中线程拿着上一期 ROE 继续判，
    而页面与告警都说「已是最新」。
    """
    gate = VersionGate()
    finance = StubFundamentalsAdapter(["A"], financials=financial_rows())
    run_financials(
        replace(deps(db, settings, finance=finance), versions=gate), ctx("601088")
    )
    assert gate.current("fundamentals") == 1


def test_valuation_job_bumps_the_gate(db, settings):
    gate = VersionGate()
    ValuationRepo(db).upsert(valuation([date(2024, 10, 24)]))
    finance = StubFundamentalsAdapter(["A"], valuation=valuation([date(2024, 10, 24), AS_OF]))
    run_valuation(
        replace(deps(db, settings, finance=finance), versions=gate), ctx("601088")
    )
    assert gate.current("fundamentals") == 1


def test_flow_job_bumps_the_gate(db, settings):
    gate = VersionGate()
    FundingFlowRepo(db).upsert(flow_rows([date(2024, 10, 24)], net=1.0e8))
    finance = StubFundamentalsAdapter(
        ["A"], flow=flow_rows([date(2024, 10, 24), AS_OF], net=1.0e8)
    )
    run_flow(
        replace(deps(db, settings, finance=finance), versions=gate), ctx("601088")
    )
    assert gate.current("fundamentals") == 1


def test_a_noop_financials_job_leaves_the_gate_alone(db, settings):
    """写 0 行还敲一次闸，换来的是一次全市场重读，而库里什么都没变（同 bars 那条）。"""
    gate = VersionGate()
    outcome = run_financials(replace(deps(db, settings), versions=gate), ctx())
    assert outcome.rows_affected == 0 and gate.current("fundamentals") == 0


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
    # 覆盖确认已经说到「全历史且到今天」，run_actions 直接跳过、不再问链：
    # 链上现在有能给公司行为的源，若不跳过就会真取回一行并写进库——row_count 为 0 才算跳过
    assert outcome.errors == ()
    assert outcome.rows_affected == 0
    assert db.row_count("corp_actions") == 0


def test_actions_job_bumps_the_gate_because_factors_moved(db, settings):
    """公司行为改的是复权口径：只 bump 自己的表不够，盘中缓存的那份日线读数同样作废。

    这一条与 `test_bars_job_bumps_the_version_gate` 是两处独立代码：`run_actions` 里那句
    bump 删掉不会让上面那枚用例变红，而它的后果是除权那天整市场的止损线照旧价判。
    """
    gate = VersionGate()
    outcome = run_actions(replace(deps(db, settings), versions=gate), ctx("601088"))
    assert outcome.rows_affected > 0
    assert gate.current("bars") == 1


def test_build_jobs_registers_only_markets_with_capability(db, settings):
    only = replace(
        settings, markets={k: v for k, v in settings.markets.items() if k in ("A", "HK")}
    )
    jobs = build_jobs(deps(db, only))
    registered = {(job.name, job.market) for job in jobs}
    # A：akshare 给日线与公司行为，eastmoney 给基本面/估值/资金流——五种全登记
    assert ("bars", "A") in registered
    assert ("actions", "A") in registered
    assert ("financials", "A") in registered
    assert ("valuation", "A") in registered
    assert ("flow", "A") in registered
    # HK：yfinance 给日线与公司行为，没有任何基本面源 ⇒ 后三种不登记
    assert ("bars", "HK") in registered
    assert ("actions", "HK") in registered
    assert ("financials", "HK") not in registered
    assert ("valuation", "HK") not in registered
    assert ("flow", "HK") not in registered


def test_build_jobs_skips_a_market_without_any_source(db, settings):
    """库里一个启用源都没有的市场：注册表这里不许抛。

    否则「某个市场暂时没源」会让整个常驻调度器起不来，连其余两个市场也不跑。
    """
    built = deps(db, settings)
    built.registry.market_sources = {"A": ("akshare", "eastmoney")}
    jobs = build_jobs(built)
    assert ("bars", "A") in {(job.name, job.market) for job in jobs}


# --- 大盘条：窗口同日线，两道闸各管一件事 ----------------------------------------------------


def one_index(settings):
    """把 A 市那组指数收成一只：默认那三只里另外两只没存过，会各自多问一次 730 天的
    回补窗口，把「这一只落后一天」那一句淹在计数里。"""
    return replace(settings, indices=IndicesCfg(codes_by_market={"A": ("sh000001",)}))


def index_rows(market: str, index_code: str, day: date) -> pl.DataFrame:
    """入库形态的一行指数日线（键名已是 `index_code`）：源形态那一列由 `sync_index` 改名。"""
    return (
        sample_bars(market=market, code=index_code, source="tencent", start=day)
        .filter(pl.col("date") == day)
        .rename({"code": "index_code"})
    )


def titles(deps_obj: JobDeps) -> set[tuple[str, str | None]]:
    return {(job.name, job.market) for job in build_jobs(deps_obj)}


def test_indices_job_is_incremental(db, settings):
    IndexDailyRepo(db).upsert(index_rows("A", "sh000001", date(2024, 10, 24)))
    adapter = StubIndexAdapter()
    outcome = run_indices(deps(db, one_index(settings), index=adapter), ctx())
    assert adapter.calls == [("A", "sh000001", AS_OF, AS_OF)]
    assert outcome.ok and outcome.rows_affected == 1
    assert IndexDailyRepo(db).latest_date("A", "sh000001") == AS_OF


def test_indices_job_skips_an_index_already_at_as_of(db, settings):
    """追平到今天就不该再问链：与 `test_fx_job_skips_a_pair_already_at_as_of` 同一条。"""
    IndexDailyRepo(db).upsert(index_rows("A", "sh000001", AS_OF))
    adapter = StubIndexAdapter()
    outcome = run_indices(deps(db, one_index(settings), index=adapter), ctx())
    assert adapter.calls == []
    assert outcome.rows_affected == 0 and outcome.errors == ()


def test_build_jobs_registers_symbol_master_only_with_service_and_capability(db, settings):
    """两道闸各钉一头：服务没注入（这个部署就没这条链）与服务在但源给不了代码表，
    后果都是「第一次跑到这里才出声」，而注册表看起来是完整的。"""
    plain = deps(db, settings)
    assert ("symbol_master", "A") not in titles(plain)
    # 服务给了但 A 市链上没人提供代码表
    assert ("symbol_master", "A") not in titles(replace(plain, symbol_master=SimpleNamespace()))
    # 链上有源但服务没装
    listing = deps(db, settings, bars=ListingBarsAdapter())
    assert ("symbol_master", "A") not in titles(listing)
    # 两边都齐才登记
    assert ("symbol_master", "A") in titles(
        replace(listing, symbol_master=SimpleNamespace())
    )


def test_build_jobs_registers_indices_only_with_repo_and_capability(db, settings):
    assert ("indices", "A") not in titles(deps(db, settings))
    # 有能力但 SyncService 没拿到指数仓库：登记了就是在 job 里撞那句 assert
    assert ("indices", "A") not in titles(
        deps(db, settings, index=StubIndexAdapter(), with_indices=False)
    )
    assert ("indices", "A") in titles(deps(db, settings, index=StubIndexAdapter()))
    # 链上没有指数源的 HK/US 不登记
    assert ("indices", "HK") not in titles(deps(db, settings, index=StubIndexAdapter()))
