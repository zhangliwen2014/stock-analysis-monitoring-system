"""测试替身与样本数据：所有测试从这里取，不在各测试文件里各写一份。

放在普通模块而非 conftest，是为了能被 `from support import ...` 显式导入——
fixture 只能注入值，没法拿来当类继承或反复构造。
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl


def sample_bars(
    market: str = "A",
    code: str = "600519",
    source: str = "akshare",
    start: date = date(2024, 1, 2),
) -> pl.DataFrame:
    """两天日线，够验列集、区间读取与入库幂等。"""
    second = start + timedelta(days=1)
    return pl.DataFrame(
        {
            "market": [market, market],
            "code": [code, code],
            "date": [start, second],
            "open": [1.0, 2.0],
            "high": [1.5, 2.5],
            "low": [0.5, 1.5],
            "close": [1.2, 2.2],
            "volume": [100.0, 200.0],
            "amount": [120.0, 440.0],
            "source": [source, source],
        }
    )


def bars_frame() -> pl.DataFrame:
    """三天标准列集：验区间读取与入库幂等。"""
    return pl.DataFrame(
        {
            "market": ["A"] * 3,
            "code": ["600519"] * 3,
            "date": [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
            "open": [1.0, 2.0, 3.0],
            "high": [1.5, 2.5, 3.5],
            "low": [0.5, 1.5, 2.5],
            "close": [1.2, 2.2, 3.2],
            "volume": [100.0, 200.0, 300.0],
            "amount": [120.0, 440.0, 960.0],
            "source": ["akshare"] * 3,
        }
    )


class StubAdapter:
    """数据源替身：outcome 是 DataFrame 就返回，是异常就抛。

    抛的是同一个异常实例——重试路径只关心类型，不关心实例。
    """

    def __init__(self, name: str, markets, outcome) -> None:
        self.name = name
        self.markets = frozenset(markets)
        self.outcome = outcome
        self.calls = 0

    def daily_bars(self, market, code, start, end):
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome
