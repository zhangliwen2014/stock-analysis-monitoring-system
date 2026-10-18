"""基本面同步用例：三类数据共用一条写库路径，各自的答案语义不同。

这里钉住的是同步这一步**会不会说谎**，不是取数怎么走（那在 `tests/datasource`）：

1. **0 行不是一次成功的同步**（D43）。链上全部源说「查无记录」时，服务层写 0 行，
   把 `empty` 与 `confirmed_empty_by` 带回来——它可以说「这几家都查过，没有」，
   不能说「这只票没有财务数据」。
2. **链断了就一行都不写**（D36 的反面）。取数失败时抛出异常、库里保持原样：
   半次同步写成一次成功，之后所有「查无基本面」的结论都建立在这次沉默上。
3. **键在入口归一**（D50）。`hk`/`700` 进来，链上问的、库里存的、屏幕上读的
   必须是同一个形态。
4. **降级与跳过都要带回调用方**（D38）。`skipped` 是「这个源不给基本面」，
   与「这个源没数据」在报告上是两件事。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from sams.core.errors import DatasourceError
from sams.datasource.fundamentals import empty_financials, empty_flow, empty_valuation
from sams.datasource.registry import Attempt, FrameResult
from sams.services.fundamentals import FundamentalSyncService
from sams.storage.repos.financials import FinancialRepo, FundingFlowRepo, ValuationRepo

D1, D2 = date(2024, 1, 2), date(2024, 6, 28)
Q1 = date(2024, 3, 31)
PUBLISHED = date(2024, 4, 26)
VALUATION_DAY = date(2024, 6, 28)


def financials_frame(**over) -> pl.DataFrame:
    row = {
        "market": "A",
        "code": "601088",
        "report_date": Q1,
        "published_date": PUBLISHED,
        "roe": 0.2,
        "revenue_yoy": 0.12,
        "profit_yoy": 0.08,
        "dividend_yield": 0.042,
        "market_cap": 6.6e11,
        "source": "akshare",
    }
    row.update(over)
    return pl.DataFrame([row])


def valuation_frame(**over) -> pl.DataFrame:
    row = {
        "market": "A",
        "code": "601088",
        "date": VALUATION_DAY,
        "pe_ttm": 20.0,
        "pb": 2.1,
        "dividend_yield": 0.042,
        "market_cap": 6.6e11,
        "source": "akshare",
    }
    row.update(over)
    return pl.DataFrame([row])


def flow_frame(**over) -> pl.DataFrame:
    row = {
        "market": "A",
        "code": "601088",
        "date": VALUATION_DAY,
        "main_net_in": 1.2e8,
        "north_net": None,
        "north_hold": None,
        "margin_bal": None,
        "source": "akshare",
    }
    row.update(over)
    return pl.DataFrame([row])


class StubRegistry:
    """只回答三条链各自的结果；降级、跳过、重试都在 `tests/datasource` 里测。"""

    def __init__(self, **results) -> None:
        self.results = results
        self.calls: list[tuple[str, tuple]] = []

    def _fetch(self, method: str, market: str, code: str, start: date, end: date) -> FrameResult:
        self.calls.append((method, (market, code, start, end)))
        outcome = self.results[method]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def fetch_financials(self, market, code, start, end):
        return self._fetch("financials", market, code, start, end)

    def fetch_valuation(self, market, code, start, end):
        return self._fetch("valuation", market, code, start, end)

    def fetch_funding_flow(self, market, code, start, end):
        return self._fetch("funding_flow", market, code, start, end)


def make_service(db, **results):
    repos = {
        "financials": FinancialRepo(db),
        "valuation": ValuationRepo(db),
        "funding_flow": FundingFlowRepo(db),
    }
    registry = StubRegistry(**results)
    service = FundamentalSyncService(
        registry, repos["financials"], repos["valuation"], repos["funding_flow"]
    )
    return service, registry, repos


def result(method: str, frame: pl.DataFrame, *, source: str = "akshare", **kw) -> FrameResult:
    defaults = dict(
        market="A",
        code="601088",
        kind={"financials": "基本面", "valuation": "按日估值", "funding_flow": "资金流"}[method],
        frame=frame,
        source=source,
        attempts=(Attempt(source, True),),
    )
    return FrameResult(**{**defaults, **kw})


# ---------- 写库 ----------


def test_sync_financials_writes_the_rows_it_was_given(db):
    service, _, repos = make_service(db, financials=result("financials", financials_frame()))
    summary = service.sync_financials("A", "601088", D1, D2)
    assert (summary.fetched, summary.written, summary.source) == (1, 1, "akshare")
    assert summary.kind == "基本面"
    assert repos["financials"].count() == 1


def test_running_the_same_window_twice_does_not_grow_the_table(db):
    service, _, repos = make_service(db, financials=result("financials", financials_frame()))
    service.sync_financials("A", "601088", D1, D2)
    again = service.sync_financials("A", "601088", D1, D2)
    assert again.written == 1
    assert repos["financials"].count() == 1, "主键覆盖写：重复同步不该长出第二行"


@pytest.mark.parametrize(
    ("method", "service_name", "maker", "counter"),
    [
        ("financials", "sync_financials", financials_frame, lambda r: r.count("A")),
        ("valuation", "sync_valuation", valuation_frame, lambda r: r.count("A")),
        ("funding_flow", "sync_flow", flow_frame, lambda r: r.count("A")),
    ],
)
def test_each_kind_lands_in_its_own_table(db, method, service_name, maker, counter):
    """三条链在写入层也是分开的：把资金流写进 `valuation_daily` 是一次没人报的错。"""
    service, _, repos = make_service(db, **{method: result(method, maker())})
    summary = getattr(service, service_name)("A", "601088", D1, D2)
    assert counter(repos[method]) == 1
    assert summary.kind == {
        "financials": "基本面",
        "valuation": "按日估值",
        "funding_flow": "资金流",
    }[method]


# ---------- 0 行与断链 ----------


def test_a_confirmed_absence_writes_nothing_and_carries_who_said_so(db):
    service, _, repos = make_service(
        db,
        financials=result(
            "financials",
            empty_financials(),
            source="sina",
            confirmed_empty_by=("sina", "akshare"),
        ),
    )
    summary = service.sync_financials("A", "601088", D1, D2)
    assert (summary.empty, summary.written) == (True, 0)
    assert summary.confirmed_empty_by == ("sina", "akshare")
    assert repos["financials"].count() == 0


@pytest.mark.parametrize(
    ("method", "service_name", "maker", "repo_name"),
    [
        ("financials", "sync_financials", empty_financials, "financials"),
        ("valuation", "sync_valuation", empty_valuation, "valuation"),
        ("funding_flow", "sync_flow", empty_flow, "funding_flow"),
    ],
)
def test_a_confirmed_absence_writes_zero_rows_into_its_own_table(
    db, method, service_name, maker, repo_name
):
    """0 行帧的形状与真帧一致，`upsert` 也接得住——但接住不等于该接：
    往 `valuation_daily` 写一行空的当日估值，下次读取就把它当成「今天查过，没有 PE」。"""
    service, _, repos = make_service(
        db, **{method: result(method, maker(), confirmed_empty_by=("akshare",))}
    )
    summary = getattr(service, service_name)("A", "601088", D1, D2)
    assert (summary.empty, summary.written) == (True, 0)
    assert repos[repo_name].count("A") == 0


def test_a_broken_chain_writes_nothing_and_leaves_the_table_alone(db):
    """库里原有的行不能被一次失败的同步抹掉，也不能被它「确认」。"""
    service, _, repos = make_service(db, financials=result("financials", financials_frame()))
    service.sync_financials("A", "601088", D1, D2)
    broken = FundamentalSyncService(
        StubRegistry(financials=DatasourceError("akshare", "503")),
        repos["financials"],
        repos["valuation"],
        repos["funding_flow"],
    )
    with pytest.raises(DatasourceError, match="503"):
        broken.sync_financials("A", "601088", D1, D2)
    assert repos["financials"].count() == 1


# ---------- 键与链的可见性 ----------


def test_the_key_is_canonical_before_the_chain_is_asked(db):
    service, registry, repos = make_service(
        db,
        valuation=result(
            "valuation",
            valuation_frame(market="HK", code="00700"),
            market="HK",
            code="00700",
        ),
    )
    summary = service.sync_valuation("hk", "700", D1, D2)
    assert registry.calls[0][1][:2] == ("HK", "00700")
    assert (summary.market, summary.code) == ("HK", "00700")
    assert repos["valuation"].latest("HK", ["00700"], D2).height == 1


def test_the_degradation_and_the_skips_travel_with_the_summary(db):
    service, _, _ = make_service(
        db,
        financials=result(
            "financials",
            financials_frame(),
            attempts=(Attempt("sina", False, error="503"), Attempt("akshare", True)),
            skipped=(("tencent", "不提供基本面"),),
        ),
    )
    summary = service.sync_financials("A", "601088", D1, D2)
    assert summary.degraded and summary.skipped == (("tencent", "不提供基本面"),)
    assert [a.source for a in summary.attempts] == ["sina", "akshare"]
