"""估值合成的读取用例（M2-D，spec §8.3）：把四路数据拼成一个分与一条结论。

这个类不做算术。分位在 `analysis/valuation/percentile`，打分在 `scoring`，价位与赔率在
`compose`，模板在 `styles`；这里只回答一件事——**每路数据取哪几个数**。四个静默出错的地点：

* **现价只有一个出处**。`valuation_daily` 没有价格列（D60），那个数只能来自过闸的日线；
  同一条序列里裸价与后复权价并存，四个价位全都与**现价**比大小，所以喂进合成的每一个
  技术值都要先折回现价口径（见 `_fold`）。少了这一步，一次除权就把止损抬到现价之上。
* **模板没问的不算**。`composite` 会拒收权重表里没有的分项；而买入闸门吃的是 PR 分位，
  它与「这个模板有没有给这一项分权重」无关（见 `_wants_pr_curve`）——闸门是那条入场规则
  唯一的否决条件，删掉权重不能顺便把它删掉。
* **分位要吃含无披露日的报告期**。逐日 PIT 意味着每一天自己筛披露日；在仓库侧先滤掉，
  那句「另有 N 期因为没有发布日而全程未被采信」就恒为 0，与同一屏上 PR 那一句对不上（D61）。
* **没有日线就整个不出**。现价是那条式子的左边，缺了它四个价位全是空的，而屏幕上
  「有一个分、只是价位没给」与「这只票我们连价都不知道」长得一模一样。

三类输入的 `as_of` 是同一个：把截至日挪回 2024 年时，分位窗口、PIT 闸门、前高区间、
资金流窗口要一起回到那时候，否则回看的每一行都被现在的库污染。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import polars as pl

from sams.analysis.valuation.compose import Plan, trade_plan
from sams.analysis.valuation.percentile import (
    DisclosedPeriod,
    Percentile,
    ValuationDay,
    metric_percentile,
    pr_percentile,
)
from sams.analysis.valuation.scoring import (
    Composite,
    Item,
    boll_position,
    composite,
    growth_score,
    ma_position,
    main_inflow_score,
    north_score,
    percentile_score,
    pr_score,
    roe_score,
    volume_follow,
    weekly_trend,
)
from sams.analysis.valuation.styles import Style, get_style, style_names
from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_code, canonical_market
from sams.core.fundamentals import ROE_BASIS_ANNUAL
from sams.services.analysis import AnalysisService
from sams.services.fundamentals import FundamentalRead, FundamentalService
from sams.storage.repos.financials import FinancialRepo, FundingFlowRepo, ValuationRepo

#: 均线位置要 MA60，所以日线那段至少要读出这么多根；模板要的根数由 `target_lookback_days` 说
_MIN_DAILY_BARS = 60
#: 一根周 K 背后是 5 个交易日：周 MA20 要的日线跨度（预热由 `AnalysisService` 负责）
_TRADING_DAYS_PER_WEEK = 5
#: 交易日 → 日历日的换算。按 244 天一个年算（A 股最少、港美股更多）：宁可多读几天，
#: 也不要因为窗口切短而把「过去 250 日高点」说成一段没查过的区间
_TRADING_DAYS_PER_YEAR = 244
#: 资金流合计的天数。拉到 10 天会让分数翻倍，而那是一段没人问过的日子
FLOW_WINDOW_DAYS = 5

#: 能打分的模板名。`cli` 不许 import 分析层（`tests/test_layering.py`），所以这份名单
#: 从这里转一道：`--style` 的帮助文本与报错时列的那份名单，来源都是 `get_style` 那一张表
STYLE_NAMES = style_names()

#: 折回现价口径时要出声的那三个数（它们的绝对值与现价比大小，不是彼此比大小）
_FOLDED_KEYS = ("周MA20", "ATR", "前高")

#: 一条分位曲线对应哪个分项。名字不能按「metric + `_percentile`」拼：`pe_ttm` 那一路
#: 在权重表里叫 `pe_percentile`，拼出来的名字不在 `ITEM_KEYS` 上，于是这一项静默不进
#: 合成，屏幕上少一栏而合成分照旧出（D66 的同一类）
_PERCENTILE_ITEM = {"pr": "pr_percentile", "pe_ttm": "pe_percentile", "pb": "pb_percentile"}


@dataclass(frozen=True)
class ValuationRead:
    """一次估值合成的全部输入、那个分、那条结论，外加「每个数分别来自哪天」。"""

    market: str
    code: str
    as_of: date
    style: Style
    #: PR 的那两个数是它决定的（D60 + D61 + D62），这里只转述，不重算
    fundamental: FundamentalRead
    composite: Composite
    plan: Plan
    #: 现价 = 过闸日线在 `price_on` 那根的**裸**收盘
    price: float | None
    price_on: date | None
    price_age_days: int | None
    #: 已经算出来的那几路分位读数（模板没问的一路不在这里，见模块开头第 2 条）
    percentiles: tuple[Percentile, ...] = ()
    #: 折回现价口径后的技术输入；None ⇒ 那一路没有数，合成侧自己会拒
    weekly_ma20: float | None = None
    atr: float | None = None
    recent_high: float | None = None
    #: 这根周 K 还没走完时它的**周期收市日**（周线那一路按日历收市日标，见 `resample`）；
    #: None ⇒ 最后一根是定型的
    open_weekly_period: date | None = None
    #: 资金流合计覆盖的那几个交易日（与库里有没有行无关，说的是「问了哪几天」）
    flow_days: tuple[date, ...] = ()
    #: 跟着数出门的口径话：折回因子、窗口不够、未走完的周期
    notes: tuple[str, ...] = ()

    def percentile(self, metric: str) -> Percentile | None:
        found = [row for row in self.percentiles if row.metric == metric]
        return found[0] if found else None


class ValuationService:
    def __init__(
        self,
        fundamentals: FundamentalService,
        analysis: AnalysisService,
        financials: FinancialRepo,
        valuation: ValuationRepo,
        flow: FundingFlowRepo,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.fundamentals = fundamentals
        self.analysis = analysis
        self.financials = financials
        self.valuation = valuation
        self.flow = flow
        self.clock = clock

    def read(
        self,
        market: str,
        code: str,
        *,
        as_of: date | None = None,
        style: Style | str = "value",
        roe_estimate: float | None = None,
        roe_basis: str = ROE_BASIS_ANNUAL,
    ) -> ValuationRead:
        """按模板给一个分与一条结论；`style` 给名字或给一份改过参数的模板都行。

        `as_of` 缺省取今天且**绝不以 None 往下传**：那个值一路到分位与 PIT 就等于
        「不设时间闸门」（与 `FundamentalService.read` 同一条红线）。
        """
        market = canonical_market(market)
        code = canonical_code(market, code)
        until = as_of if as_of is not None else self.clock.today()
        template = get_style(style) if isinstance(style, str) else style

        daily = self.analysis.series(
            market,
            code,
            start=_bar_start(until, template),
            end=until,
            indicators=_daily_indicators(template),
        )
        price = _last(daily.table, "close")
        price_on = _last(daily.table, "date")
        daily_fold = _fold(price, _last(daily.table, "close_hfq"))

        weekly = (
            self.analysis.series(
                market,
                code,
                start=_bar_start(until, template),
                end=until,
                period="week",
                indicators=("ma",),
            )
            if _wants_weekly(template)
            else None
        )
        weekly_ma20 = _weekly_line(weekly) if weekly is not None else None
        pending = weekly.pending_periods if weekly is not None else []
        notes: list[str] = []
        if pending:
            notes.append(
                f"最后一根周 K 还没走完（它那个周期的收市日是 {pending[-1]}，"
                f"在截至日 {until} 之后）：它与一根定型的周 K 同形，"
                "周MA20 与回踩区都跟着它，别当收盘结论用"
            )
        if daily_fold is not None and abs(daily_fold - 1.0) > 1e-12:
            notes.append(
                f"{'、'.join(_FOLDED_KEYS)} 由后复权序列按当日因子折回现价口径"
                f"（现价 {price:g} ÷ 当日后复权收盘 {_last(daily.table, 'close_hfq'):g} = "
                f"{daily_fold:.6f}）：不折回来，它们与现价不在同一个价空间上"
            )

        read = self.fundamentals.read(
            market, code, as_of=until, roe_estimate=roe_estimate, roe_basis=roe_basis
        )
        rows = _valuation_days(self.valuation.series(market, code))
        periods = _disclosed_periods(self.financials.periods(market, code))
        percentiles = _percentile_readings(
            template, rows, periods, until, basis=roe_basis
        )
        flow_days = tuple(daily.table["date"].to_list()[-FLOW_WINDOW_DAYS:])
        frame = (
            self.flow.load_range(market, code, flow_days[0], until)
            if flow_days
            else pl.DataFrame()
        )

        score = composite(
            _items(template, read, percentiles, daily, weekly, weekly_ma20, frame, flow_days),
            template,
        )
        lookback = int(template.param("target_lookback_days"))
        used = min(lookback, daily.rows)
        if used < lookback:
            notes.append(
                f"前高只用了 {used} 根日线（模板想要 {lookback} 根）：库里那段没铺够，"
                "或者这只票上市不足那个长度"
            )
        plan = trade_plan(
            style=template,
            price=price,
            pr=read.pr.value,
            basis=read.pr.basis,
            pr_percentile=_gate_reading(percentiles),
            weekly_ma20=weekly_ma20,
            atr=_folded(_last(daily.table, "atr"), daily_fold),
            recent_high=_folded(_window_high(daily.table, used), daily_fold),
            lookback_days=used,
        )
        return ValuationRead(
            market=market,
            code=code,
            as_of=until,
            style=template,
            fundamental=read,
            composite=score,
            plan=plan,
            price=price,
            price_on=price_on,
            price_age_days=None if price_on is None else (until - price_on).days,
            percentiles=tuple(percentiles),
            weekly_ma20=weekly_ma20,
            atr=_folded(_last(daily.table, "atr"), daily_fold),
            recent_high=_folded(_window_high(daily.table, used), daily_fold),
            open_weekly_period=pending[-1] if pending else None,
            flow_days=flow_days,
            notes=tuple(notes),
        )


# --- 取哪几个数 ---------------------------------------------------------------------------


def _bar_start(until: date, template: Style) -> date:
    """日线那一段往回读到哪：取「模板要的根数」与「MA60 要的根数」里更长的那个，换成日历日。

    周 MA20 要的跨度按 `_TRADING_DAYS_PER_WEEK` 再乘一次：一根周 K 吃五个交易日，
    而周线那一路与日线那一路用的是同一个 `start`（两条序列的预热都从那天往后算）。
    """
    need = max(int(template.param("target_lookback_days")), _MIN_DAILY_BARS)
    if _wants_weekly(template):
        need = max(need, 21 * _TRADING_DAYS_PER_WEEK)
    return until - timedelta(days=math.ceil(need * 365 / _TRADING_DAYS_PER_YEAR) + 30)


def _daily_indicators(template: Style) -> tuple[str, ...]:
    """只算模板会问的那几组指标：一屏二十列没人读，而多算一组就是多一栏没人负责的解释。"""
    asked = set(template.asked())
    names = []
    if "ma_position" in asked:
        names.append("ma")
    if "boll_position" in asked:
        names.append("boll")
    if "volume_follow" in asked:
        names.append("volume_ma")
    if template.param("stop_kind") == "atr2":
        names.append("atr")
    return tuple(names)


def _wants_weekly(template: Style) -> bool:
    """周线那一路被两件事问起：周线趋势那个分项，以及贴着周 MA20 的止损/回踩区。"""
    return (
        "weekly_trend" in template.weights
        or template.param("stop_kind") == "weekly_ma20"
        or template.param("entry_rule") == "weekly_ma20_pullback"
    )


def _wants_pr_curve(template: Style) -> bool:
    """分项要不要算，与入场闸门要不要它，是两问（模块开头第 2 条）。"""
    return "pr_percentile" in template.weights or template.param("entry_rule") == "near_fair_low"


def _percentile_readings(
    template: Style,
    rows: list[ValuationDay],
    periods: list[DisclosedPeriod],
    until: date,
    *,
    basis: str,
) -> list[Percentile]:
    window = int(template.param("percentile_window_years"))
    asked = set(template.asked())
    out: list[Percentile] = []
    if _wants_pr_curve(template):
        out.append(pr_percentile(rows, periods, until, basis=basis, window_years=window))
    for metric in ("pe_ttm", "pb"):
        if _PERCENTILE_ITEM[metric] in asked:
            out.append(metric_percentile(rows, metric, until, window_years=window))
    return out


def _gate_reading(readings: list[Percentile]) -> Percentile | None:
    """喂进买入闸门的那个读数：只能是 PR 那条曲线。

    模板没问它而闸门问它（`_wants_pr_curve` 的第二条），此时 `near_fair_low` 仍要说得出
    闸门过没过；一条也没有时给 None，合成那侧会拒并说明「没有历史分位读数」。
    """
    return next((row for row in readings if row.metric == "pr"), None)


def _items(
    template: Style,
    read: FundamentalRead,
    readings: list[Percentile],
    daily,
    weekly,
    weekly_ma20: float | None,
    flow: pl.DataFrame,
    flow_days: tuple[date, ...],
) -> list[Item]:
    """按模板问到的那几项逐个算，顺序与 `asked()` 无关（`composite` 自己按模板排）。"""
    asked = set(template.asked())
    table = daily.table
    items: list[Item] = []
    if "pr" in asked:
        items.append(pr_score(read.pr.value, reason=read.pr.reason))
    for row in readings:
        key = _PERCENTILE_ITEM[row.metric]
        if key in asked:
            items.append(percentile_score(row))
    if "roe" in asked:
        items.append(roe_score(read.roe, period=str(read.period) if read.period else ""))
    if "growth" in asked:
        items.append(growth_score(read.revenue_yoy, read.profit_yoy))
    if "ma_position" in asked:
        items.append(
            ma_position(
                close=_last(table, "close_hfq"),
                ma20=_last(table, "ma20"),
                ma60=_last(table, "ma60"),
            )
        )
    if "weekly_trend" in asked and weekly is not None:
        items.append(
            weekly_trend(
                _last(weekly.table, "close_hfq"),
                _last(weekly.table, "ma20"),
                _before_last(weekly.table, "ma20"),
            )
        )
    if "boll_position" in asked:
        items.append(
            boll_position(
                _last(table, "close_hfq"), _last(table, "boll_up"), _last(table, "boll_low")
            )
        )
    if "volume_follow" in asked:
        items.append(
            volume_follow(
                _pct_change(table, "close_hfq"), _last(table, "volume"), _last(table, "volume_ma5")
            )
        )
    no_flow = _no_flow_row(flow, flow_days)
    if "main_inflow" in asked:
        main = _total(flow, "main_net_in")
        items.append(main_inflow_score(main, read.market_cap, reason=no_flow))
    if "north_bound" in asked:
        items.append(north_score(_total(flow, "north_net"), read.market_cap, reason=no_flow))
    return items


def _no_flow_row(frame: pl.DataFrame, days: tuple[date, ...]) -> str:
    """库里这段一行都没有时的那句话：下一步是 `sams sync flow`，不是换源。

    有行而那一列全空时返回空串，让 `scoring` 那句「该源不给这一列（U25）」生效——
    两者的下一步命令不同，混成一句就是两头都指错。
    """
    if frame.height:
        return ""
    span = f"{days[0]}~{days[-1]}" if days else "这段"
    return (
        f"库里 {span} 没有资金流行：这一项没有数据，不给分也不补一个「中性」的 50。"
        "下一步 `sams sync flow`"
    )


# --- 价空间与取数小工具 -------------------------------------------------------------------


def _fold(price: float | None, hfq_last: float | None) -> float | None:
    """后复权 → 现价的换算系数。两个数齐了才有它，否则 None（合成侧自己会拒）。"""
    if price is None or hfq_last is None or hfq_last <= 0 or price <= 0:
        return None
    return price / hfq_last


def _folded(value: float | None, factor: float | None) -> float | None:
    if value is None or factor is None:
        return None
    return value * factor


def _weekly_line(weekly) -> float | None:
    """周 MA20 折回现价口径：用它**自己那条序列**的当日因子，不借日线的。

    后复权的绝对水平跟着读取窗口的锚走（`anchored_on`），两条序列的锚可能不同一天；
    而 `现价 ÷ 当日后复权收盘` 这个比值在同一序列内自洽，折出来的数与锚无关。
    """
    return _folded(
        _last(weekly.table, "ma20"),
        _fold(_last(weekly.table, "close"), _last(weekly.table, "close_hfq")),
    )


def _window_high(table, bars: int) -> float | None:
    """窗口内最高的那根后复权收盘（还没折回现价口径）。

    用复权而不是裸价：跨除权日的裸收盘价不可比——那次「下跌」是分红，不是有人付过更低的价。
    """
    if table.height == 0 or bars <= 0 or "close_hfq" not in table.columns:
        return None
    return table["close_hfq"].tail(bars).max()


def _last(table, column: str) -> Any:
    if table.height == 0 or column not in table.columns:
        return None
    return table[column][-1]


def _before_last(table, column: str) -> Any:
    if table.height < 2 or column not in table.columns:
        return None
    return table[column][-2]


def _pct_change(table, column: str) -> float | None:
    """最后一根相对上一根的涨跌幅（在后复权口径里算：跨除权日时裸价那个差是分红）。"""
    now, before = _last(table, column), _before_last(table, column)
    if now is None or before is None or before <= 0:
        return None
    return now / before - 1.0


def _valuation_days(frame) -> list[ValuationDay]:
    return [
        ValuationDay(day=row["date"], pe_ttm=row.get("pe_ttm"), pb=row.get("pb"))
        for row in frame.to_dicts()
    ]


def _disclosed_periods(frame) -> list[DisclosedPeriod]:
    return [
        DisclosedPeriod(
            report_date=row["report_date"],
            roe=row.get("roe"),
            published_date=row.get("published_date"),
        )
        for row in frame.to_dicts()
    ]


def _total(frame: pl.DataFrame, column: str) -> float | None:
    """那一列的合计；**一行非空值都没有时是 None**，不是 0。

    0 在资金流的刻度上意思是「没有净流入」，而 None 意思是「我们没这件事的数据」——
    把后者写成前者，屏幕上那行会把「没同步」读成「主力在出货」。
    """
    if frame.height == 0 or column not in frame.columns:
        return None
    values: Sequence[float] = frame[column].drop_nulls().to_list()
    if not values:
        return None
    return float(sum(values))
