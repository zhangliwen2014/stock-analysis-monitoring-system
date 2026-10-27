"""周线、月线、季线与年线由日线现算（spec §8.2）。只聚合，不重新复权、不落库。

三条口径：

1. **一根周期 K 的名字是那个周期在日历上的收市日（周五 / 月末 / 季末 / 年末），不是这段里
   最后一根 K 线。** 周五休市那一周仍然叫那个周五——否则「本周」会随长假、停电漂移，
   而周线指标吃的正是「第 N 周」。实际最后一根另存 `last` 列，两者不一致就说明那一周
   提前收市（这正是 `tests/cli` 那条「锚点移动」的诚实写法在周期上的版本）。
2. **没走完的那一周不冒充收盘周 K**（`pending` 列，判据是周期收市日在 `as_of` 之后）。
   少了这个标记，周线 MA5 里会混进一根只走了三天的 K 线，而它在图上与一根完整周 K
   完全同形。给标记而不是拒绝：读数的人常常就是要看本周走到哪了，但屏幕必须知道它
   还没走完。
3. **入口与出口都是 `AdjustedSeries`**。因子已在日线乘好（`adjusted_series()` 是唯一的
   放行入口，D42），重采样只是把已定型的日 K 聚合成周/月 K。自己「先聚合再乘因子」
   的写法有两种，两种在一周中间除权时给出不同的数，而两种都在图上完全正常。

`as_of` 没有默认值（同 `interval_return` 的 `today`，D44）：省略它等于让「补采的三周
历史」被当成三根已收市的周 K，而那只发生在时钟没人传的时候。
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from sams.analysis.adjust import AdjustedSeries
from sams.core.errors import ValidationError

#: 聚合方式。`first`/`max`/`min`/`last`/`sum` 全是**尾部聚合**：不看周期之后的任何一根。
#: 身份列（`market`/`code`）按 first 取：一条序列本来就只有一只票，这是常数不是选择。
#: `source` 按 **last** 取，并且它说的只是「最后一根是谁写的」——一根周 K 可以由两个源
#: 拼成（补采时换了备源），把它说成一整周都来自那个源，是 D43 那条「0 行不等于没分红」
#: 在源这一侧的同一个错法。
_AGG = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum",
    "amount": "sum",
    "market": "first",
    "code": "first",
    "source": "last",
    #: 周期末的累计因子：周内除过权时它是「这一周结束时的复权倍数」，与 `*_hfq` 那几列同口径
    "adj_factor": "last",
    #: 换手率按日相加就是这一周的换手：它是「当日成交量 / 流通股本」，一周买卖两轮就是两轮
    "turnover": "sum",
    #: `bars_daily` 的每一行都带这两列，`load()` 是 `SELECT *`，所以真实序列一定带着它们
    "adjust": "first",  # 读路径按 `adjust = 'raw'` 过滤，这是常数
    "updated_at": "last",  # 最后一根是谁在什么时候写的，与 `source` 同一口径
}
WEEK = "week"
MONTH = "month"
QUARTER = "quarter"
YEAR = "year"
#: `resample()` 能处理的周期名名单；`services` 层组装 `PERIODS` 取值表时引用这一份，
#: 不重新写字符串——两处各管一份会在有人只改一处的时候静默漂移
RESAMPLE_PERIODS: tuple[str, ...] = (WEEK, MONTH, QUARTER, YEAR)


def _require_series(series: object) -> AdjustedSeries:
    if not isinstance(series, AdjustedSeries):
        raise ValidationError(
            f"重采样只接受过闸的 `AdjustedSeries`（现在是 {type(series).__name__}）："
            "裸日线或自己乘过因子的帧都不算数，复权序列的唯一出口是 "
            "`analysis.adjust.adjusted_series()`（D42）"
        )
    return series


def _month_end(day: date) -> date:
    following = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return following - timedelta(days=1)


def _period_end(day: date, period: str) -> date:
    """该日所在周期的**日历收市日**：周五（周）、该季/该年的日历末日、该月最后一天。"""
    if period == WEEK:
        return day + timedelta(days=4 - day.weekday())
    if period == QUARTER:
        return _month_end(day.replace(month=((day.month - 1) // 3) * 3 + 3))
    if period == YEAR:
        return date(day.year, 12, 31)
    return _month_end(day)


def _week_group(day: date) -> date:
    """所在周的周一。用它而不是 ISO 周号做键：跨年那一周（12 月末~1 月初）按 ISO 会
    跳成上一年或下一年的第 1 周，而「同一周」在数据里必须是同一个键。"""
    return day - timedelta(days=day.weekday())


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _quarter_group(day: date) -> date:
    """所在季的 1 号（1/4/7/10 月）：季是三个自然月一组，不做「滚动三个月」。"""
    return day.replace(month=((day.month - 1) // 3) * 3 + 1, day=1)


def _year_group(day: date) -> date:
    return date(day.year, 1, 1)


#: 每个周期的分组键，形状统一是「该周期第一天」：同周期的日线必须落进同一个键，
#: 而键本身不参与输出（名字由 `_period_end` 从日历上算）
_GROUP_OF = {
    WEEK: _week_group,
    MONTH: _month_start,
    QUARTER: _quarter_group,
    YEAR: _year_group,
}


def resample(series: object, period: str, *, as_of: date) -> AdjustedSeries:
    """把一条已过闸的日线序列聚成周线、月线、季线或年线，返回同一种类型（指标层可直接吃）。

    输出的列与日线那套同名（`open/high/low/close`、`*_hfq`、`volume`、`amount`），
    外加五个周期才有的字段：

    * `date` —— 周期的日历收市日（口径 1）
    * `first` / `last` —— 这一周期里第一根与最后一根日线
    * `bars` —— 聚进来几根日线（一根周 K 只有一根日线，通常是停牌或长假）
    * `pending` —— 周期收市日还在 `as_of` 之后（口径 2）
    """
    if period not in RESAMPLE_PERIODS:
        raise ValidationError(
            f"重采样周期只认 {'、'.join(f'`{p}`' for p in RESAMPLE_PERIODS)}，"
            f"收到的是 {period!r}："
            "写成 \"weekly\" 静默当成 week 等于让调用方以为自己要的东西被给了"
        )
    source = _require_series(series)
    frame = source.frame
    if "date" not in frame.columns or "close" not in frame.columns:
        raise ValidationError("重采样需要 `date` 与 `close` 两列：这条序列不是 K 线形状")

    ordered = frame.sort("date")
    days = ordered["date"].to_list()
    group_of = _GROUP_OF[period]
    columns = list(ordered.columns)
    aggregates = []
    for column in columns:
        if column == "date":
            continue
        how = _aggregation_for(column)
        aggregates.append(getattr(pl.col(column), how)().alias(column))
    grouped = (
        ordered.with_columns(pl.Series("_group", [group_of(d) for d in days], dtype=pl.Date))
        .group_by("_group")
        .agg(
            [
                *aggregates,
                pl.col("date").min().alias("first"),
                pl.col("date").max().alias("last"),
                pl.len().alias("bars"),
            ]
        )
        .sort("_group")
    )
    ends = [_period_end(g, period) for g in grouped["_group"].to_list()]
    out = grouped.with_columns(
        pl.Series("date", ends, dtype=pl.Date),
        pl.Series("pending", [end > as_of for end in ends], dtype=pl.Boolean),
    ).drop("_group")
    keep = ["date"] + [c for c in columns if c != "date"] + ["first", "last", "bars", "pending"]
    return AdjustedSeries(
        frame=out.select(keep),
        covered_by=source.covered_by,
        covered_through=source.covered_through,
        factors=source.factors,
    )


def _aggregation_for(column: str) -> str:
    """这一列按哪种方式聚合。`*_hfq` 跟着它的基础列走（口径 3：只聚合，不重新乘因子）。"""
    base = column[: -len("_hfq")] if column.endswith("_hfq") else column
    if base not in _AGG:
        raise ValidationError(
            f"不认识列 {column}：重采样不知道该按哪种口径聚合它，"
            "静默丢掉等于让一根周 K 少了最高价或最低价"
        )
    return _AGG[base]
