"""技术面读模型（S4-A / D251–D253）：选股用的六个字段，按快照日现算，不落表。

三条口径写在这里：

* **只到快照日**：`bars.load(end=day)` 与 `list_market(until=day)` 是同一把 PIT 锁；
  快照日之后的 K 线与除权都不该改写那一天的 MA/RSI。
* **必须过复权闸**：逐票走 `adjusted_series()`，没有公司行为覆盖确认的票不出数——
  裸价算出的均线看起来完全正常，而它跨越除权日时是错的（D36）。
* **逐字段报缺口**：30 根日线算得出 MA20，算不出 MA250；不能让长窗口的缺口把短窗口
  一起抹掉，也不能用一个「无数」盖住两种原因。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import polars as pl

from sams.analysis.adjust import AdjustedSeries, adjusted_series
from sams.analysis.guard import is_covered
from sams.analysis.indicators import ma, rsi
from sams.core.codes import canonical_market
from sams.core.errors import NotAdjustable
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.corp_actions_coverage import CorpActionsCoverageRepo
from sams.storage.repos.symbols import SymbolRepo

#: 一次最多取这么多根：MA250 与 52 周位置都要 250 根，留 10 根给预热
WINDOW = 260

FIELDS: tuple[str, ...] = (
    "ma20_gap",
    "ma60_gap",
    "ma250_gap",
    "rsi14",
    "vol_ratio_5d",
    "pos_52w",
)

#: 每个字段至少要多少根收盘。缺口说的是**这一列**要几根，不是「全部要 250 根」
MIN_BARS = {
    "ma20_gap": 20,
    "ma60_gap": 60,
    "ma250_gap": 250,
    "rsi14": 15,
    "vol_ratio_5d": 6,
    "pos_52w": 250,
}

_SHARE_CHANGING = frozenset({"split", "bonus", "transfer", "rights"})

#: 选股池不是 universe：这些票没人替它们排队，能做的动作是点开那一行（D283）
NO_COVERAGE = "公司行为覆盖未确认，无法复权：打开该股页面会登记后台补采"
NOT_ADJUSTABLE = (
    "公司行为无法折算或有未标记跳变，复权序列被拒绝：先补齐该票的公司行为"
)
VOLUME_SPLIT = "近 6 个交易日有送转/拆股，量比口径不成立"
FLAT_52W = "最近 250 个交易日收盘没有波动：52 周位置没有分母"


@dataclass(frozen=True)
class TechnicalSnapshot:
    values: pl.DataFrame
    #: (字段, 代码) → 这一格为什么没有数
    reasons: dict[tuple[str, str], str]
    #: 至少有一个技术字段出数的票数
    computable: int


class TechnicalService:
    def __init__(
        self,
        bars: BarRepo,
        actions: CorpActionRepo,
        coverage: CorpActionsCoverageRepo,
        symbols: SymbolRepo,
    ) -> None:
        self.bars = bars
        self.actions = actions
        self.coverage = coverage
        self.symbols = symbols

    def snapshot(
        self, market: str, codes: Sequence[str], day: date
    ) -> TechnicalSnapshot:
        market = canonical_market(market)
        wanted = list(codes)
        if not wanted:
            return TechnicalSnapshot(_empty(()), {}, 0)
        if market != "A":
            # 港美股日线与公司行为覆盖都不够：宁可整片「无数」，也不开一个数字看着正常的口子
            reasons = {
                (name, code): "技术面字段 v0 只对 A 股开放"
                for name in FIELDS
                for code in wanted
            }
            return TechnicalSnapshot(_empty(()), reasons, 0)

        loaded = self.bars.load(market, wanted, end=day)
        if loaded.is_empty():
            reasons = {
                (name, code): _need(name) for name in FIELDS for code in wanted
            }
            return TechnicalSnapshot(_empty(()), reasons, 0)

        by_code = {
            str(key[0]): frame
            for key, frame in loaded.partition_by("code", as_dict=True).items()
        }
        action_rows = self.actions.list_market(market, until=day)
        actions_by: dict[str, pl.DataFrame] = {}
        if not action_rows.is_empty():
            actions_by = {
                str(key[0]): frame
                for key, frame in action_rows.partition_by("code", as_dict=True).items()
            }
        spans = self.coverage.for_market(market)
        listed = {
            str(row["code"]): row.get("listed_on")
            for row in self.symbols.list_market(market).iter_rows(named=True)
        }

        rows: list[dict] = []
        reasons: dict[tuple[str, str], str] = {}
        computable = 0
        for code in wanted:
            frame = by_code.get(code)
            values = dict.fromkeys(FIELDS)
            if frame is None:
                for name in FIELDS:
                    reasons[(name, code)] = _need(name)
                rows.append({"code": code, **values})
                continue
            tail = frame.sort("date").tail(WINDOW)
            last = tail["date"].to_list()[-1]
            code_spans = spans.get(code, [])
            if is_covered(code_spans, start=tail["date"].to_list()[0], end=last) is None:
                for name in FIELDS:
                    reasons[(name, code)] = NO_COVERAGE
                rows.append({"code": code, **values})
                continue
            try:
                series = adjusted_series(
                    market,
                    code,
                    tail,
                    actions_by.get(code, _empty_actions()),
                    coverage=code_spans,
                    today=day,
                    listed_on=listed.get(code),
                )
            except NotAdjustable:
                for name in FIELDS:
                    reasons[(name, code)] = NOT_ADJUSTABLE
                rows.append({"code": code, **values})
                continue

            values, cell_reasons = _values(series, tail, actions_by.get(code))
            reasons.update({(name, code): why for name, why in cell_reasons.items()})
            if any(value is not None for value in values.values()):
                computable += 1
            rows.append({"code": code, **values})

        return TechnicalSnapshot(
            values=pl.DataFrame(
                rows,
                schema={"code": pl.Utf8, **{name: pl.Float64 for name in FIELDS}},
            ),
            reasons=reasons,
            computable=computable,
        )


def _values(
    series: AdjustedSeries, raw: pl.DataFrame, actions: pl.DataFrame | None
) -> tuple[dict[str, float | None], dict[str, str]]:
    """一条过闸序列的末根读数。返回该票每格的值与「为什么这一格没有数」。"""
    out: dict[str, float | None] = dict.fromkeys(FIELDS)
    why: dict[str, str] = {}
    closes = series.frame["close_hfq"].to_list()
    if not closes:
        return out, {name: _need(name) for name in FIELDS}

    averages = ma(series, 20, 60, 250).tail(1).row(0, named=True)
    last_close = closes[-1]
    for name, window in (("ma20_gap", 20), ("ma60_gap", 60), ("ma250_gap", 250)):
        average = averages.get(f"ma{window}")
        if average is None:
            why[name] = _need(name)
        elif average <= 0:
            why[name] = "均线算出了非正值：比值没有意义"
        else:
            out[name] = last_close / average - 1

    if len(closes) < MIN_BARS["rsi14"]:
        why["rsi14"] = _need("rsi14")
    else:
        value = rsi(series, 14).tail(1)["rsi14"].item()
        if value is None:
            why["rsi14"] = "整段没有涨跌：RSI 没有分母"
        else:
            out["rsi14"] = value

    if "volume" not in raw.columns:
        why["vol_ratio_5d"] = "日线没有成交量列"
    elif len(raw) < MIN_BARS["vol_ratio_5d"]:
        why["vol_ratio_5d"] = _need("vol_ratio_5d")
    elif _share_change_in(raw, actions):
        why["vol_ratio_5d"] = VOLUME_SPLIT
    else:
        volumes = raw.sort("date")["volume"].to_list()[-6:]
        previous = volumes[:-1]
        mean = sum(previous) / len(previous) if previous else 0.0
        if mean <= 0:
            why["vol_ratio_5d"] = "前 5 日均量为 0：量比没有分母"
        else:
            out["vol_ratio_5d"] = volumes[-1] / mean

    if len(closes) < MIN_BARS["pos_52w"]:
        why["pos_52w"] = _need("pos_52w")
    else:
        window = closes[-250:]
        low, high = min(window), max(window)
        if high == low:
            why["pos_52w"] = FLAT_52W
        else:
            out["pos_52w"] = (last_close - low) / (high - low)

    return out, why


def _share_change_in(raw: pl.DataFrame, actions: pl.DataFrame | None) -> bool:
    if actions is None or actions.is_empty():
        return False
    start = raw.sort("date")["date"].to_list()[-6]
    window = actions.filter(pl.col("ex_date") >= start)
    if window.is_empty():
        return False
    return any(
        row["kind"] in _SHARE_CHANGING and (row["ratio"] or 1.0) != 1.0
        for row in window.iter_rows(named=True)
    )


def _need(name: str) -> str:
    return (
        f"日线不足 {MIN_BARS[name]} 根：技术面字段按快照日及之前的日线现算，"
        "先跑 `sams sync bars --market A --code <代码>`"
    )


def _empty(codes: Sequence[str]) -> pl.DataFrame:
    return pl.DataFrame(
        {"code": pl.Series(list(codes), dtype=pl.Utf8)}
        | {name: pl.Series([], dtype=pl.Float64) for name in FIELDS}
    )


def _empty_actions() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "market": pl.Utf8,
            "code": pl.Utf8,
            "ex_date": pl.Date,
            "kind": pl.Utf8,
            "cash_per_share": pl.Float64,
            "ratio": pl.Float64,
            "price": pl.Float64,
            "src": pl.Utf8,
            "note": pl.Utf8,
            "updated_at": pl.Datetime("us", "UTC"),
        }
    )


