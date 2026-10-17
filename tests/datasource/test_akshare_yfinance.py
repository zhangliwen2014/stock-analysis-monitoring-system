from __future__ import annotations

import sys
from datetime import date

import polars as pl
import pytest

from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift, RateLimited
from sams.datasource.akshare_cn import AkshareAdapter
from sams.datasource.yfinance_us import (
    YfinanceAdapter,
    flatten_columns,
    normalize_history,
    yfinance_fetch,
)

RECORDED_CN = pl.DataFrame(
    {
        "日期": [date(2024, 1, 2), date(2024, 1, 3)],
        "开盘": [1800.0, 1810.0],
        "最高": [1820.0, 1830.0],
        "最低": [1790.0, 1800.0],
        "收盘": [1810.0, 1815.0],
        "成交量": [30000.0, 28000.0],
        "成交额": [5.4e9, 5.1e9],
        "涨跌幅": [1.1, 0.3],
    }
)


def pandas_history(rows: int = 2):
    import pandas as pd

    index = pd.to_datetime(["2024-01-02", "2024-01-03"][:rows])
    return pd.DataFrame(
        {
            "Open": [1800.0, 1810.0][:rows],
            "High": [1820.0, 1830.0][:rows],
            "Low": [1790.0, 1800.0][:rows],
            "Close": [1810.0, 1815.0][:rows],
            "Volume": [30000.0, 28000.0][:rows],
        },
        index=index,
    )


def test_akshare_recorded_frame_becomes_standard_bars():
    adapter = AkshareAdapter(fetch=lambda symbol, start, end: RECORDED_CN)
    out = adapter.daily_bars("A", "600519", date(2024, 1, 2), date(2024, 1, 3))
    assert out["close"].to_list() == [1810.0, 1815.0]
    assert out["source"].unique().to_list() == ["akshare"]
    assert "涨跌幅" not in out.columns, "多余列不该混进标准列集"


def test_akshare_fetch_receives_bare_symbol_and_range():
    seen: dict = {}

    def spy(symbol, start, end):
        seen.update(symbol=symbol, start=start, end=end)
        return RECORDED_CN

    AkshareAdapter(fetch=spy).daily_bars("A", "600519", date(2024, 1, 2), date(2024, 1, 3))
    assert seen["symbol"] == "600519"
    assert seen["start"] == date(2024, 1, 2)


def test_akshare_pandas_frame_is_accepted():
    import pandas as pd

    frame = pd.DataFrame(RECORDED_CN.to_pandas())
    adapter = AkshareAdapter(fetch=lambda symbol, start, end: frame)
    assert adapter.daily_bars("A", "600519", date(2024, 1, 2), date(2024, 1, 3)).height == 2


def test_akshare_empty_and_drift_are_distinguishable():
    with pytest.raises(EmptyResponse):
        AkshareAdapter(fetch=lambda s, a, b: pl.DataFrame()).daily_bars(
            "A", "600519", date(2024, 1, 2), date(2024, 1, 3)
        )
    with pytest.raises(FieldDrift):
        AkshareAdapter(fetch=lambda s, a, b: pl.DataFrame({"日期": [date(2024, 1, 2)]})).daily_bars(
            "A", "600519", date(2024, 1, 2), date(2024, 1, 3)
        )


def test_akshare_throttle_is_classified():
    def throttled(symbol, start, end):
        raise RuntimeError("抱歉，您发出的请求过频")

    with pytest.raises(RateLimited):
        AkshareAdapter(fetch=throttled).daily_bars(
            "A", "600519", date(2024, 1, 2), date(2024, 1, 3)
        )


def test_yfinance_normalizes_history_and_marks_source():
    adapter = YfinanceAdapter(fetch=lambda symbol, start, end: pandas_history())
    out = adapter.daily_bars("US", "AAPL", date(2024, 1, 2), date(2024, 1, 3))
    assert out["market"].unique().to_list() == ["US"]
    assert out["close"].to_list() == [1810.0, 1815.0]
    assert out["source"].unique().to_list() == ["yfinance"]
    assert out["amount"].null_count() == 2, "yfinance 不给成交额，填 null 而不是 0"


def test_yfinance_translates_hk_code_to_yahoo_symbol():
    seen: dict = {}

    def spy(symbol, start, end):
        seen["symbol"] = symbol
        return pandas_history()

    YfinanceAdapter(fetch=spy).daily_bars("HK", "00700", date(2024, 1, 2), date(2024, 1, 3))
    assert seen["symbol"] == "00700.HK"


def test_yfinance_multiindex_columns_are_flattened():
    import pandas as pd

    frame = pandas_history().iloc[:1]
    frame.columns = pd.MultiIndex.from_product([frame.columns, ["AAPL"]])
    flatten_columns(frame)
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert normalize_history(frame, market="US", code="AAPL").height == 1


def test_yfinance_empty_history_is_empty_response():
    import pandas as pd

    with pytest.raises(EmptyResponse):
        normalize_history(pd.DataFrame(), market="US", code="AAPL")


def test_yfinance_silent_failure_is_classified_as_retriable():
    """history() 抛出的 429 必须归入 RateLimited，路由才会退避重试而不是当成没数据。"""

    def throttled(symbol, start, end):
        raise RuntimeError("Too Many Requests. Rate limited. Try after a while.")

    adapter = YfinanceAdapter(fetch=throttled)
    with pytest.raises(RateLimited):
        adapter.daily_bars("US", "AAPL", date(2024, 1, 2), date(2024, 1, 3))


def test_yfinance_fetch_moves_end_one_day_because_api_is_exclusive(monkeypatch):
    import types

    calls = {}

    class FakeTicker:
        def __init__(self, symbol):
            calls["symbol"] = symbol

        def history(self, **kwargs):
            calls.update(kwargs)
            return None

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=FakeTicker))
    assert yfinance_fetch("AAPL", date(2024, 1, 2), date(2024, 1, 3)) is None
    assert calls["start"] == "2024-01-02" and calls["end"] == "2024-01-04"


def test_yfinance_rejects_other_markets():
    with pytest.raises(ValueError, match="不支持"):
        YfinanceAdapter(fetch=lambda s, a, b: pandas_history()).daily_bars(
            "A", "600519", date(2024, 1, 2), date(2024, 1, 3)
        )


def test_unclassified_failure_becomes_datasource_error():
    def broken(symbol, start, end):
        raise KeyError("Close")

    with pytest.raises(DatasourceError):
        YfinanceAdapter(fetch=broken).daily_bars("US", "AAPL", date(2024, 1, 2), date(2024, 1, 3))


def test_akshare_volume_is_normalised_to_shares():
    """单位契约：akshare 的成交量是「手」，出库必须是「股」。

    这条断言读的是录制数据里的真实口径，不是代码注释——源改成给股时它会红。
    """
    adapter = AkshareAdapter(fetch=lambda *_: RECORDED_CN)
    out = adapter.daily_bars("A", "600519", date(2024, 1, 2), date(2024, 1, 3))
    assert out["volume"].to_list() == [3_000_000.0, 2_800_000.0]


def test_yfinance_volume_is_already_shares():
    """美股不需要换算：把两处口径都写死，才有人会发现哪天源变了。"""
    out = normalize_history(pandas_history(), market="US", code="AAPL")
    assert out["volume"].to_list() == [30000.0, 28000.0]
