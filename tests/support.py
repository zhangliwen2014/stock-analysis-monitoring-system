"""测试替身与样本数据：所有测试从这里取，不在各测试文件里各写一份。

放在普通模块而非 conftest，是为了能被 `from support import ...` 显式导入——
fixture 只能注入值，没法拿来当类继承或反复构造。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import polars as pl

from sams.core.errors import EmptyResponse


class FrozenClock:
    """可控时钟：时间只能由测试推进。

    有了它，「会话过期」「重放两次结果相同」这类断言才不必靠 sleep 或墙钟碰运气——
    生产代码里的时间一律来自注入的 Clock，所以这里冻住它就能钉死行为。
    """

    name = "frozen"

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def now_utc(self) -> datetime:
        return self.moment

    def today(self) -> date:
        return self.moment.date()

    def advance(self, **kwargs) -> None:
        self.moment += timedelta(**kwargs)


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


def sample_actions(
    market: str = "A",
    code: str = "601088",
    source: str = "akshare",
    ex_date: date = date(2024, 7, 8),
) -> pl.DataFrame:
    """一条「10 派 22.60 元」的派息记录（D34 口径：每股 2.26 元）。"""
    return pl.DataFrame(
        {
            "market": [market],
            "code": [code],
            "ex_date": [ex_date],
            "kind": ["dividend"],
            "cash_per_share": [2.26],
            "ratio": [None],
            "price": [None],
            "src": [source],
            "note": ["10派22.60元(含税,扣税后20.34元)"],
        }
    )


class StubActionsAdapter(StubAdapter):
    """日线 + 公司行为双能力替身：`corp_actions_full_history` 走默认的全历史口径。"""

    corp_actions_full_history = True

    def __init__(self, name, markets, outcome, actions_outcome=None) -> None:
        super().__init__(name, markets, outcome)
        self.actions_outcome = (
            sample_actions(source=name) if actions_outcome is None else actions_outcome
        )
        self.actions_calls = 0

    def corp_actions(self, market, code, start, end):
        self.actions_calls += 1
        if isinstance(self.actions_outcome, BaseException):
            raise self.actions_outcome
        return self.actions_outcome


def sample_minute_series(
    market: str = "A",
    code: str = "600519",
    day: date = date(2026, 10, 28),
    source: str = "tencent",
) -> pl.DataFrame:
    """三条分时：量与额都是**累计**（腾讯给的就是累计，服务层负责差分成每分钟）。"""
    return pl.DataFrame(
        {
            "market": [market] * 3,
            "code": [code] * 3,
            "date": [day] * 3,
            "time": ["09:30", "09:31", "09:32"],
            "price": [1354.98, 1353.00, 1352.10],
            "volume": [153700.0, 318600.0, 400000.0],
            "amount": [208260426.0, 430459525.34, 540000000.0],
            "prev_close": [1355.90] * 3,
            "source": [source] * 3,
        }
    )


def sample_minute_bars(
    market: str = "A",
    code: str = "600519",
    day: date = date(2026, 10, 28),
    source: str = "tencent",
) -> pl.DataFrame:
    """三根分钟 K：`clock` 是这一根的结束时刻，`amount` 留空（单位没核准）。"""
    return pl.DataFrame(
        {
            "market": [market] * 3,
            "code": [code] * 3,
            "date": [day] * 3,
            "clock": ["10:55", "11:00", "11:05"],
            "open": [1331.35, 1335.25, 1333.10],
            "high": [1335.83, 1335.83, 1333.10],
            "low": [1331.02, 1333.02, 1331.00],
            "close": [1335.25, 1333.09, 1331.41],
            "volume": [66900.0, 66900.0, 11000.0],
            "amount": [None, None, None],
            "source": [source] * 3,
        }
    )


class StubMinuteAdapter(StubAdapter):
    """分时 + 分钟 K 双能力替身：默认照腾讯的意愿表关档（分时答 A/HK、分钟 K 只答 A）。"""

    minute_series_markets = frozenset({"A", "HK"})
    minute_bars_markets = frozenset({"A"})

    def __init__(
        self,
        name: str,
        markets=("A",),
        outcome=None,
        *,
        series=None,
        bars=None,
        series_markets=None,
        bars_markets=None,
    ) -> None:
        super().__init__(name, markets, outcome)
        self.series_frame = series if series is not None else sample_minute_series()
        self.bars_frame = bars if bars is not None else sample_minute_bars()
        if series_markets is not None:
            self.minute_series_markets = frozenset(series_markets)
        if bars_markets is not None:
            self.minute_bars_markets = frozenset(bars_markets)
        self.extra: tuple[int, int] | None = None

    def minute_series(self, market, code, start, end):
        self.calls += 1
        if market not in self.minute_series_markets:
            raise EmptyResponse(self.name, f"分时不答 {market}")
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.series_frame

    def minute_bars(self, market, code, start, end, *, scale, count):
        self.calls += 1
        self.extra = (scale, count)
        if market not in self.minute_bars_markets:
            raise EmptyResponse(self.name, f"分钟K不答 {market}")
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.bars_frame
