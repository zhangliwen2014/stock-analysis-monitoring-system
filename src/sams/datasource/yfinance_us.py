"""美股 / 港股日线与公司行为源：yfinance。

盘中快照在 M3 接入，届时新增 SnapshotAdapter 协议而不是往本类里塞方法——
保持「一个类一个职责」。公司行为挂在同一个类上则是另一回事：它和日线是同一个源的
两种能力（D38），拆成两个 kind 会让「yfinance 挂了」变成两条互不相干的告警。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

import polars as pl

from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift
from sams.datasource.actions import empty_actions, normalize_actions
from sams.datasource.base import (
    classify_error,
    code_in_market,
    normalize_bars,
    parses_as_date,
)
from sams.datasource.sources import source

#: 标准列 → 源列（yfinance 首字母大写）。
#: 这里刻意不映射 amount：`Ticker.history()` 不提供成交额，写个假列名只会让
#: 「港美股成交额不可得」这件事在代码里隐身——规则引擎因此不得对 US/HK 用金额阈值。
COLUMN_MAP = {
    "date": "date",
    "open": "Open",
    "high": "High",
    "low": "Low",
    "close": "Close",
    "volume": "Volume",
}

#: 索引列在 reset_index 之后必须认出来：yfinance 各版本给的名字不一样
DATE_INDEX_NAMES = {"date", "datetime", "index", ""}


def yfinance_fetch(symbol: str, start: date, end: date):
    """走 Ticker.history 而不是 yf.download。

    `download()` 把失败（含 429）吞进内部 ctx.errors 只打印日志，返回空帧，
    调用方就分不清「停牌」与「被限流」；history() 会真的抛出来。
    history 的 end 是开区间，而本系统的区间参数按闭区间语义传进来。
    """
    import yfinance as yf

    return yf.Ticker(symbol).history(
        start=start.strftime("%Y-%m-%d"),
        end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
        auto_adjust=False,
        actions=False,
    )


def flatten_columns(frame) -> None:
    """yfinance 单标的也可能返回 MultiIndex 列，统一压平成首字母大写的单层列名。"""
    if hasattr(frame.columns, "nlevels") and frame.columns.nlevels > 1:
        frame.columns = frame.columns.get_level_values(0)


def normalize_history(frame, *, market: str, code: str) -> pl.DataFrame:
    """录制/抓取到的 pandas 历史 → 标准列集（纯函数，测试入口）。"""
    flatten_columns(frame)
    if frame.empty:
        raise EmptyResponse("yfinance", f"{market} {code} 区间无数据")
    prepared = frame.reset_index()
    prepared.columns = [str(c) for c in prepared.columns]
    pl_frame = pl.from_pandas(prepared)
    date_column = None
    for candidate in pl_frame.columns:
        if candidate.lower() in DATE_INDEX_NAMES and parses_as_date(pl_frame, candidate):
            date_column = candidate
            break
    if date_column is None:
        # 装着 0/1/2 的整数索引名字再像日期也不算：那是改版，不是「这个区间没数据」
        raise FieldDrift(
            "yfinance",
            f"{market} {code} 找不到可解析为日期的列，实际列 {pl_frame.columns}",
        )
    if date_column != "date":
        pl_frame = pl_frame.rename({date_column: "date"})
    return normalize_bars(
        pl_frame,
        market=market,
        code=code,
        source="yfinance",
        column_map=COLUMN_MAP,
    )


def yfinance_actions(symbol: str):
    """真实网络调用：某只票的全历史公司行为（分红、拆股、资本利得分配）。

    走 `get_actions(period="max")` 而不是 `Ticker.actions`：后者是前者的 property，
    直接调函数让「取数」这件事在测试里是一个可替换的边界。索引带交易所时区，
    所以日历日必须在转成 polars 之前按那个时区取——先转 UTC 再取 date 会让
    美东下午公布的除息日落到前一天。
    """
    import yfinance as yf

    return yf.Ticker(symbol).get_actions(period="max")


#: `Ticker.actions` 的列名（Yahoo 侧首字母大写，各版本大小写不稳）
_DIVIDEND_COLUMN = "dividends"
_SPLIT_COLUMN = "stock splits"
_GAINS_COLUMN = "capital gains"
#: 折不出数的列一律进 `note`，让 D39 的 unknown 记录带上可复核的原文
_ACTION_COLUMNS = (_DIVIDEND_COLUMN, _SPLIT_COLUMN, _GAINS_COLUMN)


def _local_date(value) -> date | None:
    """带交易所时区的时间戳 → **当地**日历日。"""
    if value is None or value != value:  # None / NaN
        return None
    return value.date() if hasattr(value, "date") else value


def expand_actions(frame) -> pl.DataFrame:
    """录制/抓取到的 pandas actions → 长表（一条公司行为一行，纯函数，测试入口）。"""
    flatten_columns(frame)
    lowered = {str(column).lower(): column for column in frame.columns}
    usable = [name for name in _ACTION_COLUMNS if name in lowered]
    if not usable:
        raise FieldDrift(
            "yfinance",
            f"公司行为响应里没有 Dividends/Stock Splits/Capital Gains 任何一列，"
            f"实际列 {list(frame.columns)}",
        )
    prepared = frame.reset_index()
    prepared.columns = [str(c) for c in prepared.columns]
    date_column = None
    for candidate in prepared.columns:
        if candidate.lower() in DATE_INDEX_NAMES:
            date_column = candidate
            break
    if date_column is None:
        raise FieldDrift("yfinance", f"公司行为响应找不到日期列，实际列 {list(prepared.columns)}")

    rows: list[dict] = []
    for record in prepared.to_dict("records"):
        ex_date = _local_date(record[date_column])
        if ex_date is None:
            continue
        dividends = _number(record.get(lowered.get(_DIVIDEND_COLUMN)))
        gains = _number(record.get(lowered.get(_GAINS_COLUMN)))
        cash = round((dividends or 0.0) + (gains or 0.0), 10)
        if cash > 0:
            note = "分红" if dividends and not gains else "分红 + 资本利得分配"
            rows.append(
                {"ex_date": ex_date, "kind": "dividend", "cash_per_share": cash,
                 "ratio": None, "price": None, "note": note}
            )
        split = _number(record.get(lowered.get(_SPLIT_COLUMN)))
        if split and split != 1.0:
            # Yahoo 的 2.0 读作「1 股变 2 股」，与 D34 的 ratio（除权后/除权前）同向
            rows.append(
                {"ex_date": ex_date, "kind": "split", "cash_per_share": None,
                 "ratio": split, "price": None, "note": f"拆合股 {split}:1"}
            )
    schema = {
        "ex_date": pl.Date,
        "kind": pl.Utf8,
        "cash_per_share": pl.Float64,
        "ratio": pl.Float64,
        "price": pl.Float64,
        "note": pl.Utf8,
    }
    if not rows:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows, schema=schema).sort(["ex_date", "kind"])


def _number(value) -> float | None:
    """NaN / None / 非数一律当「这一列没有值」，而不是 0。

    0 与「没有」在因子推导上是两件事：0 会算出一个恒等于 1 的因子并宣称「这天除过权」。
    """
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out


class YfinanceAdapter:
    name = "yfinance"
    markets = frozenset({"US", "HK"})
    #: `get_actions(period="max")` 一次给全史，所以确认能写到今天（D36）
    corp_actions_full_history = True

    def __init__(
        self,
        fetch: Callable[[str, date, date], object] = yfinance_fetch,
        actions_fetch: Callable[[str], object] = yfinance_actions,
    ) -> None:
        self._fetch = fetch
        self._actions_fetch = actions_fetch

    def daily_bars(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        symbol = code_in_market(market, code)
        try:
            return normalize_history(self._fetch(symbol, start, end), market=market, code=code)
        except DatasourceError:
            raise  # 已经是本域异常，别二次归类
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc

    def corp_actions(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        symbol = code_in_market(market, code)
        try:
            expanded = expand_actions(self._actions_fetch(symbol))
            if expanded.is_empty():
                # 一只从不派息的票是真实存在的状态，不是故障：给它一次成功的空答复，
                # 覆盖标记（D36）才写得下去
                return empty_actions()
            windowed = expanded.filter((pl.col("ex_date") >= start) & (pl.col("ex_date") <= end))
            if windowed.is_empty():
                return empty_actions()
            return normalize_actions(windowed, market=market, code=code, source=self.name)
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc


source("yfinance", note="yfinance 港美股日线 + 公司行为（Yahoo）")(YfinanceAdapter)
