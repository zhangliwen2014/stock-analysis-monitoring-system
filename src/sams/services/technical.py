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
from datetime import date, datetime

import polars as pl

from sams.analysis.adjust import AdjustedSeries, adjusted_series
from sams.analysis.guard import is_covered
from sams.analysis.indicators import ma, rsi
from sams.core.codes import canonical_market
from sams.core.errors import NotAdjustable
from sams.services.analysis import SeriesRead
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.corp_actions_coverage import CorpActionsCoverageRepo
from sams.storage.repos.symbol_backfills import ACTIONS, BARS
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

#: 个股页那张卡要的九个数（D356）：前四个是均线**水平**，选股那六个是相对均线的偏离。
CARD_FIELDS: tuple[str, ...] = (
    "ma5",
    "ma10",
    "ma20",
    "ma60",
    "chg_20d",
    "chg_60d",
    "rsi14",
    "vol_ratio_5d",
    "pos_52w",
)

_SHARE_CHANGING = frozenset({"split", "bonus", "transfer", "rights"})

#: 理由只讲事实（D337）：能补哪一种活由 `fixes` 说，命令由命令行自己附加。
#: 选股池不是 universe，这些票没人在队列里替它们登记——选股页现在会替它们排（按钮）。
NO_COVERAGE = "公司行为覆盖未确认，无法复权"
NOT_ADJUSTABLE = "公司行为无法折算或有未标记跳变，复权序列被拒绝"
VOLUME_SPLIT = "近 6 个交易日有送转/拆股，量比口径不成立"
FLAT_52W = "最近 250 个交易日收盘没有波动：52 周位置没有分母"


@dataclass(frozen=True)
class TechnicalSnapshot:
    values: pl.DataFrame
    #: (字段, 代码) → 这一格为什么没有数
    reasons: dict[tuple[str, str], str]
    #: (字段, 代码) → 队列里哪种活能补上这一格（D337）；空串表示补不了
    fixes: dict[tuple[str, str], str]
    #: 至少有一个技术字段出数的票数
    computable: int


@dataclass(frozen=True)
class SymbolMetrics:
    """个股页技术面卡的读数，连同它自己的 as-of 与本地写入时刻（D356）。"""

    values: dict[str, float | None]
    #: 缺数的那几格为什么没有数
    reasons: dict[str, str]
    #: 表里末根 K 线的日期
    as_of: date
    #: 那根 K 线本地最后写入的时刻（`bars_daily.updated_at`）；老库没这列时为 None
    written_at: datetime | None


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
            return TechnicalSnapshot(_empty(()), {}, {}, 0)
        if market != "A":
            # 港美股日线与公司行为覆盖都不够：宁可整片「无数」，也不开一个数字看着正常的口子
            reasons = {
                (name, code): "技术面字段 v0 只对 A 股开放"
                for name in FIELDS
                for code in wanted
            }
            return TechnicalSnapshot(_empty(()), reasons, dict.fromkeys(reasons, ""), 0)

        loaded = self.bars.load(market, wanted, end=day)
        if loaded.is_empty():
            reasons = {
                (name, code): _need(name) for name in FIELDS for code in wanted
            }
            return TechnicalSnapshot(_empty(()), reasons, dict.fromkeys(reasons, BARS), 0)

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
        fixes: dict[tuple[str, str], str] = {}
        computable = 0
        for code in wanted:
            frame = by_code.get(code)
            values = dict.fromkeys(FIELDS)
            if frame is None:
                for name in FIELDS:
                    reasons[(name, code)] = _need(name)
                    fixes[(name, code)] = BARS
                rows.append({"code": code, **values})
                continue
            tail = frame.sort("date").tail(WINDOW)
            last = tail["date"].to_list()[-1]
            code_spans = spans.get(code, [])
            if is_covered(code_spans, start=tail["date"].to_list()[0], end=last) is None:
                _mark(reasons, fixes, code, NO_COVERAGE, ACTIONS)
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
                _mark(reasons, fixes, code, NOT_ADJUSTABLE, ACTIONS)
                rows.append({"code": code, **values})
                continue

            values, cell_reasons, cell_fixes = _values(
                series, tail, actions_by.get(code)
            )
            for name, why in cell_reasons.items():
                reasons[(name, code)] = why
                fixes[(name, code)] = cell_fixes.get(name, "")
            if any(value is not None for value in values.values()):
                computable += 1
            rows.append({"code": code, **values})

        return TechnicalSnapshot(
            values=pl.DataFrame(
                rows,
                schema={"code": pl.Utf8, **{name: pl.Float64 for name in FIELDS}},
            ),
            reasons=reasons,
            fixes=fixes,
            computable=computable,
        )


def symbol_metrics(read: SeriesRead, actions: pl.DataFrame) -> SymbolMetrics:
    """个股页那张技术面卡（D356）：从页面已经拿到的 `SeriesRead` 里取九个数。

    与图表同基：均线直接读 `read.table` 的末行——那是图上那条线的同一列，不是另起
    一次预热算出来的第二个数。价格类字段一律用后复权列；量比用**不复权**成交量，
    因为送转那天的「股」与新股的「股」不是同一个单位。

    调用方要用 `series(..., indicators=("ma", "rsi"), windows={"ma": (5, 10, 20, 60)})`
    拿这份读数——卡片只读已经算好的列，不自己再算一遍。
    """
    row = read.table.sort("date").row(-1, named=True)
    frame = read.series.frame.sort("date")
    closes = frame["close_hfq"].to_list()
    out: dict[str, float | None] = dict.fromkeys(CARD_FIELDS)
    why: dict[str, str] = {}

    for name, window in (("ma5", 5), ("ma10", 10), ("ma20", 20), ("ma60", 60)):
        value = row.get(name)
        if value is None:
            why[name] = f"日线不足 {window} 根：均线按库内全部日线递推"
        else:
            out[name] = float(value)

    for name, back, window in (("chg_20d", 21, 20), ("chg_60d", 61, 60)):
        if len(closes) < back:
            why[name] = f"日线不足 {back} 根：{window} 日涨跌要末根与第 {back} 根收盘"
        elif closes[-back] <= 0:
            why[name] = f"第 {back} 根后复权收盘不是正数：比值没有意义"
        else:
            out[name] = closes[-1] / closes[-back] - 1

    # 这三格与选股共用同一套算术（送转/拆股后的量比、52 周位置的分母、RSI 的涨跌）
    shared, cell_why, _ = _values(read.series, frame, actions)
    for name in ("rsi14", "vol_ratio_5d", "pos_52w"):
        out[name] = shared[name]
        if shared[name] is None:
            why[name] = _card_reason(name, cell_why[name])

    return SymbolMetrics(
        values=out,
        reasons=why,
        as_of=row["date"],
        written_at=frame.row(-1, named=True).get("updated_at"),
    )


def _card_reason(name: str, why: str) -> str:
    """选股那句「按快照日」在个股页没有快照：同一件事换成页面上的说法。"""
    if why != _need(name):
        return why
    return {
        "rsi14": f"日线不足 {MIN_BARS['rsi14']} 根：RSI14 至少要 15 根收盘",
        "vol_ratio_5d": f"日线不足 {MIN_BARS['vol_ratio_5d']} 根：量比要末根与前 5 日均量",
        "pos_52w": f"日线不足 {MIN_BARS['pos_52w']} 根：52 周位置要 250 根后复权收盘",
    }[name]


def _mark(
    reasons: dict[tuple[str, str], str],
    fixes: dict[tuple[str, str], str],
    code: str,
    reason: str,
    fix: str,
) -> None:
    """一只票整片没有数：六个字段同一句理由、同一种活。"""
    for name in FIELDS:
        reasons[(name, code)] = reason
        fixes[(name, code)] = fix


def _values(
    series: AdjustedSeries, raw: pl.DataFrame, actions: pl.DataFrame | None
) -> tuple[dict[str, float | None], dict[str, str], dict[str, str]]:
    """一条过闸序列的末根读数。返回该票每格的值、「为什么这一格没有数」与能补的活。"""
    out: dict[str, float | None] = dict.fromkeys(FIELDS)
    why: dict[str, str] = {}
    fix: dict[str, str] = {}
    closes = series.frame["close_hfq"].to_list()
    if not closes:
        return (
            out,
            {name: _need(name) for name in FIELDS},
            dict.fromkeys(FIELDS, BARS),
        )

    averages = ma(series, 20, 60, 250).tail(1).row(0, named=True)
    last_close = closes[-1]
    for name, window in (("ma20_gap", 20), ("ma60_gap", 60), ("ma250_gap", 250)):
        average = averages.get(f"ma{window}")
        if average is None:
            why[name] = _need(name)
            fix[name] = BARS
        elif average <= 0:
            why[name] = "均线算出了非正值：比值没有意义"
        else:
            out[name] = last_close / average - 1

    if len(closes) < MIN_BARS["rsi14"]:
        why["rsi14"] = _need("rsi14")
        fix["rsi14"] = BARS
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
        fix["vol_ratio_5d"] = BARS
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
        fix["pos_52w"] = BARS
    else:
        window = closes[-250:]
        low, high = min(window), max(window)
        if high == low:
            why["pos_52w"] = FLAT_52W
        else:
            out["pos_52w"] = (last_close - low) / (high - low)

    return out, why, fix


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
    return f"日线不足 {MIN_BARS[name]} 根：技术面字段按快照日及之前的日线现算"


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


