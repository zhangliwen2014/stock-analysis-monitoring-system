from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from sams.core.errors import DatasourceError
from sams.datasource.registry import EMPTY_BARS, Attempt, FetchResult
from sams.services.sync import SyncService
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.symbols import SymbolRepo

D1, D2 = date(2024, 1, 2), date(2024, 1, 3)


def bars_frame(source: str = "yfinance") -> pl.DataFrame:
    return pl.DataFrame(
        {
            "market": ["US", "US"],
            "code": ["AAPL", "AAPL"],
            "date": [D1, D2],
            "open": [1.0, 2.0],
            "high": [1.5, 2.5],
            "low": [0.5, 1.5],
            "close": [1.2, 2.2],
            "volume": [100.0, 200.0],
            "amount": [None, None],
            "source": [source, source],
        }
    )


class StubRegistry:
    """只回答路由结果，好让同步用例与降级逻辑分开测试。"""

    def __init__(self, result: FetchResult | Exception) -> None:
        self.result = result
        self.calls: list[tuple] = []

    def fetch_daily(self, market, code, start, end) -> FetchResult:
        self.calls.append((market, code, start, end))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make_service(db, settings, result) -> tuple[SyncService, StubRegistry, BarRepo, SymbolRepo]:
    registry = StubRegistry(result)
    bars, symbols = BarRepo(db), SymbolRepo(db)
    return SyncService(registry, bars, symbols, settings.markets), registry, bars, symbols


def ok_result(**kw) -> FetchResult:
    defaults = dict(
        market="US", code="AAPL", frame=bars_frame(), source="yfinance",
        attempts=(Attempt("yfinance", True),),
    )
    return FetchResult(**{**defaults, **kw})


def test_sync_writes_bars_and_creates_symbol(db, settings):
    svc, registry, bars, symbols = make_service(db, settings, ok_result())
    summary = svc.sync_daily("US", "AAPL", D1, D2)

    assert registry.calls == [("US", "AAPL", D1, D2)]
    assert (summary.fetched, summary.written) == (2, 2)
    assert summary.source == "yfinance" and summary.latest_date == D2
    assert bars.count("US", "AAPL") == 2
    row = symbols.get("US", "AAPL")
    assert row["currency"] == "USD" and row["list_status"] == "L"


def test_a_lowercase_market_lands_on_the_canonical_key(db, settings):
    """`--market us` 会在主档造一个 `us` 的行，而 K 线按源给的 `US` 入库。

    同一只票从此有两个键：主档查不到它的币种，`sams health` 数不到它的 K 线。
    """
    svc, registry, bars, symbols = make_service(db, settings, ok_result())
    summary = svc.sync_daily("us", "AAPL", D1, D2)
    assert registry.calls == [("US", "AAPL", D1, D2)]
    assert summary.market == "US"
    assert bars.count("US", "AAPL") == 2
    assert symbols.get("US", "AAPL")["currency"] == "USD"
    assert symbols.get("us", "AAPL") is None


def test_rerun_is_idempotent(db, settings):
    svc, _, bars, _ = make_service(db, settings, ok_result())
    svc.sync_daily("US", "AAPL", D1, D2)
    svc.sync_daily("US", "AAPL", D1, D2)
    assert bars.count("US", "AAPL") == 2, "连续两次同步行数必须一致（验收 2）"


def test_degraded_route_is_reported_not_hidden(db, settings):
    result = ok_result(
        source="eastmoney",
        frame=bars_frame("eastmoney"),
        attempts=(Attempt("akshare", False, 3, "429"), Attempt("eastmoney", True)),
    )
    svc, _, _, _ = make_service(db, settings, result)
    summary = svc.sync_daily("US", "AAPL", D1, D2)
    assert summary.degraded and summary.source == "eastmoney"
    assert [a.source for a in summary.attempts] == ["akshare", "eastmoney"]


def test_all_sources_empty_writes_nothing(db, settings):
    empty = ok_result(frame=EMPTY_BARS, source="", attempts=(), empty_everywhere=True)
    svc, _, bars, symbols = make_service(db, settings, empty)
    summary = svc.sync_daily("US", "AAPL", D1, D2)
    assert summary.empty and summary.written == 0
    assert bars.count() == 0
    assert symbols.get("US", "AAPL") is None, "没有数据就别造主档"


def test_existing_symbol_metadata_survives(db, settings):
    SymbolRepo(db).upsert(
        pl.DataFrame(
            [
                {
                    "market": "US",
                    "code": "AAPL",
                    "name": "Apple Inc.",
                    "currency": "USD",
                    "sector": "科技",
                }
            ]
        )
    )
    svc, _, _, _ = make_service(db, settings, ok_result())
    svc.sync_daily("US", "AAPL", D1, D2)
    row = SymbolRepo(db).get("US", "AAPL")
    assert row["name"] == "Apple Inc." and row["sector"] == "科技"


def test_total_failure_propagates_the_router_error(db, settings):
    svc, _, bars, _ = make_service(db, settings, DatasourceError("registry", "所有源均失败"))
    with pytest.raises(DatasourceError, match="所有源均失败"):
        svc.sync_daily("US", "AAPL", D1, D2)
    assert bars.count() == 0


def test_market_without_currency_config_is_rejected(db, settings):
    svc, _, _, _ = make_service(db, settings, ok_result())
    with pytest.raises(DatasourceError, match="JP"):
        svc.sync_daily("JP", "AAPL", D1, D2)


def test_currency_comes_from_market_config_not_the_adapter(db, settings):
    svc, _, _, _ = make_service(db, settings, ok_result())
    assert svc.currency_of("A", "600519") == "CNY" and svc.currency_of("HK", "00700") == "HKD"


def test_currency_is_overridden_by_the_code_segment(db, settings):
    """市场级默认币种的两个真实例外：B 股与港股人民币双柜台。

    错币会顺着 symbols.currency 一路进到展示与换算，而且没有任何一处会报错。
    """
    svc, _, _, _ = make_service(db, settings, ok_result())
    assert svc.currency_of("A", "900901") == "USD"  # 沪市 B 股
    assert svc.currency_of("A", "200011") == "HKD"  # 深市 B 股
    assert svc.currency_of("HK", "80700") == "CNY"  # 腾讯人民币双柜台
    assert svc.currency_of("HK", "08100") == "HKD"  # 08xxx 是港股创业板，仍是 HKD
