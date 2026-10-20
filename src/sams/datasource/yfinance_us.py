"""美股 / 港股日线与公司行为源：yfinance。

盘中快照在 M3 接入，届时新增 SnapshotAdapter 协议而不是往本类里塞方法——
保持「一个类一个职责」。公司行为挂在同一个类上则是另一回事：它和日线是同一个源的
两种能力（D38），拆成两个 kind 会让「yfinance 挂了」变成两条互不相干的告警。

估值（`valuation`）在这里只是**当日一行**的快照，理由与它不给 `financials` 是同一个，
两条都写在 `datasource/test_yfinance_valuation.py` 的模块文档里：Yahoo 给不出
「这一期是什么时候公开的」，而 D61 的 PIT 闸门只认披露日。

汇率（`fx_rate`）也挂在这个类上，是同一条理由：它与日线是同一个源的第四种能力（D38），
拆成第五个 kind 会让「Yahoo 挂了」变成两条互不相干的告警。它没有市场维度，所以不走
`chain_for` 那条按 `(market, code)` 路由的链，由 `registry.fetch_fx` 自己按注册顺序问一遍。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift
from sams.datasource.actions import empty_actions, normalize_actions
from sams.datasource.base import (
    classify_error,
    code_in_market,
    normalize_bars,
    parses_as_date,
    to_date_expr,
)
from sams.datasource.fundamentals import normalize_valuation
from sams.datasource.fx import normalize_fx
from sams.datasource.sources import source

logger = logging.getLogger("sams.datasource")

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


#: `Ticker.info` 里能直接当「今天的倍数/市值」用的键。`dividendYield` **刻意不在内**：
#: 它的单位在 Yahoo/yfinance 版本之间漂移过（0.52 是 0.52% 还是 52%），本机被限流无法实测，
#: 而一个未核实的比率口径进库的后果是那一列静默差一百倍（D58）。留 null，读取侧会说明。
VALUATION_KEYS = {"pe_ttm": "trailingPE", "pb": "priceToBook", "market_cap": "marketCap"}
#: 快照日期先折算成交易所当地日历日（`snapshot_date`），归一层看到的只是一个日期列
SNAPSHOT_DATE_COLUMN = "snapshotDate"
VALUATION_MAP = {"date": SNAPSHOT_DATE_COLUMN, **VALUATION_KEYS}
_TIMEZONE_KEY = "exchangeTimezoneName"


def yfinance_info(symbol: str) -> Mapping[str, Any]:
    """真实网络调用：某只票的当前概况（含 trailingPE/priceToBook/marketCap）。

    走 `Ticker.info` 而不是 `fast_info`：后者不给市值之外的比率的日期线索，而这一条链
    必须要知道「这个 PE 属于哪一个交易日」。
    """
    import yfinance as yf

    return yf.Ticker(symbol).info


def snapshot_date(info: Mapping[str, Any]) -> date:
    """这份快照属于**哪一个交易日**：交易所当地的日历日。

    两个坑，都只可能安静地错：采集日（周末跑同步会把周五的估值写成周六），
    以及 UTC 日（美东下午的快照在 UTC 已经是次日——`corp_actions` 那条同一形状的坑）。
    时间戳不带时区时直接拒绝：这一列猜错一天，按日 PE 序列就整段平移，M2-D 的分位跟着偏。
    """
    stamp = info.get("regularMarketTime")
    tz_name = info.get("exchangeTimezoneName")
    if stamp is None or tz_name is None:
        raise EmptyResponse(
            "yfinance",
            f"估值快照里没有时区信息（regularMarketTime={stamp!r}、"
            f"{_TIMEZONE_KEY}={tz_name!r}），无法确定它属于哪个交易日",
        )
    zone = ZoneInfo(str(tz_name))
    if hasattr(stamp, "tzinfo"):
        moment = stamp.to_pydatetime() if hasattr(stamp, "to_pydatetime") else stamp
        if moment.tzinfo is None:
            raise EmptyResponse("yfinance", "估值快照的时间戳不带时区，猜不出当地是几号")
        return moment.astimezone(zone).date()
    try:
        return datetime.fromtimestamp(float(stamp), zone).date()
    except (TypeError, ValueError, OSError) as exc:
        raise EmptyResponse("yfinance", f"估值快照的时间戳读不出来：{stamp!r}") from exc


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
        info_fetch: Callable[[str], Mapping[str, Any]] = yfinance_info,
    ) -> None:
        self._fetch = fetch
        self._actions_fetch = actions_fetch
        self._info_fetch = info_fetch

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
            # 全历史源不按窗口切片：窗口外的派息已经拿到手了，丢掉它就是让库与
            # 「确认至今天的全历史覆盖」那条记录说反话
            return normalize_actions(expanded, market=market, code=code, source=self.name)
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc


    def valuation(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        """当日一行的估值快照：`Ticker.info` 是快照不是序列。

        只给一行意味着它填不进 M2-D 要的「PE 历史分位」——那句话写在模块文档里，
        不写在这里假装能力更多。`percent_columns=()`：倍数不参与 ÷100（D58），
        而股息率那一列压根不映射（单位未核实），进库即 null。
        """
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        symbol = code_in_market(market, code)
        try:
            info = self._info_fetch(symbol)
            if not isinstance(info, Mapping):
                raise FieldDrift(
                    "yfinance",
                    f"{market} {code} 的估值概况不是键值结构（拿到 {type(info).__name__}）："
                    "接口改版或被代理页拦下，这一页读不出任何倍数",
                )
            row = {SNAPSHOT_DATE_COLUMN: snapshot_date(info)}
            row.update({key: info.get(key) for key in VALUATION_KEYS.values()})
            if all(row.get(key) is None for key in VALUATION_KEYS.values()):
                # 一个数都没有的一行不是「这只票没估值」，是这一页没读到东西：
                # 写成 0 行会让降级链把它当成一次可信的「查无记录」并落下覆盖确认（D43）
                raise EmptyResponse(
                    "yfinance",
                    f"{market} {code} 的估值概况里 trailingPE/priceToBook/marketCap 全为空",
                )
            frame = normalize_valuation(
                pl.DataFrame([row]),
                market=market,
                code=code,
                source=self.name,
                column_map=VALUATION_MAP,
                percent_columns=(),
            )
            return frame.filter(pl.col("date").is_between(start, end))
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc

    def fx_rate(self, base: str, quote: str, start: date, end: date) -> pl.DataFrame:
        """1 单位 base = ? 单位 quote：`{base}{quote}=X` 是 Yahoo 的外汇代码。

        只按请求的方向拼代码，绝不反过来请求再取倒数：方向写反的症状是「大约对」，
        而持仓那一屏的换汇影响会带着这个错一直算下去（§5.4 规则 2）。
        周末与假日没有报价是这条序列的正常形状，这里既不补值也不报错。
        """
        symbol = f"{base}{quote}=X"
        try:
            frame = self._fetch(symbol, start, end)
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc
        if frame is None or frame.empty:
            raise EmptyResponse("yfinance", f"{symbol} 在 {start}~{end} 没有数据")
        flatten_columns(frame)
        prepared = frame.reset_index()
        prepared.columns = [str(column) for column in prepared.columns]
        pl_frame = pl.from_pandas(prepared)
        date_column = next(
            (
                column
                for column in pl_frame.columns
                if column.lower() in DATE_INDEX_NAMES and parses_as_date(pl_frame, column)
            ),
            None,
        )
        if date_column is None or "Close" not in pl_frame.columns:
            raise FieldDrift(
                "yfinance",
                f"{symbol} 的响应里读不出日期列或收盘价，实际列 {pl_frame.columns}",
            )
        return normalize_fx(
            pl.DataFrame(
                {
                    "date": pl_frame.select(to_date_expr(pl.col(date_column))).to_series(),
                    "base": [base] * pl_frame.height,
                    "quote": [quote] * pl_frame.height,
                    "rate": pl_frame["Close"],
                }
            ),
            base=base,
            quote=quote,
            source=self.name,
        )


source("yfinance", note="yfinance 港美股日线 + 公司行为 + 当日估值快照 + 汇率（Yahoo）")(
    YfinanceAdapter
)
