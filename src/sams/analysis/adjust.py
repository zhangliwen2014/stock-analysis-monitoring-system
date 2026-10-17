"""后复权序列的现算（spec §5.5 / D34 / D35）。

**库里只有裸价，这一层不写库。** 因子依赖「除权前那根 K 线的收盘价」，把它存下来等于
把某个时点的价格当成永久事实——来源回填改一次历史价，存着的因子就漂了；现算慢一点，
换来的是任何时候重算都得到同一个答案。

一个公式覆盖全部公司行为（D34）：

    除权参考价 = (前收 − 每股收到现金 + 每股新股应付现金) / 股数乘数
    当日因子   = 前收 / 除权参考价
    hfq_t      = raw_t × ∏(除权日 ≤ t 的当日因子)

拆股、送股、转增、配股在同一个式子里退化成同一件事，所以四种行为不必各写一套算法。
`ratio = 1`（只有派息）时它化成 (P − D)/P；`cash = 0`（只有拆股）时化成 1/ratio。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import polars as pl

from sams.core.errors import NotAdjustable


@dataclass(frozen=True)
class DayFactor:
    """一个除权日的合成因子。同一天既派息又送股时合并成一个，不连乘两次。"""

    ex_date: date
    prev_close: float | None
    ex_ref: float | None
    factor: float


def ex_reference(
    prev_close: float, *, cash: float, paid: float, share_multiplier: float
) -> float:
    """除权参考价。`cash` 是每股收到的钱，`paid` 是每股旧股需要为新股付的钱。"""
    return (prev_close - cash + paid) / share_multiplier


def group_by_ex_date(actions: pl.DataFrame) -> dict[date, dict[str, float]]:
    """逐条行为 → 「这一天总共动了多少钱、多少股」。

    股数乘数把送转、配股、拆股的新增比例**相加**而不是相乘：10 送 3 转 2 是 1.5 倍
    股数，不是 1.3 × 1.2。配股的 `price` 是每股新股应付的现金，所以摊进参考价时
    乘的是新股比例（`ratio − 1`），即「每股旧股要付多少钱」。
    """
    days: dict[date, dict[str, float]] = {}
    for row in actions.iter_rows(named=True):
        day = days.setdefault(row["ex_date"], {"cash": 0.0, "paid": 0.0, "extra": 0.0})
        ratio = row["ratio"] if row["ratio"] is not None else 1.0
        day["cash"] += row["cash_per_share"] or 0.0
        if row["kind"] == "rights":
            day["paid"] += (row["price"] or 0.0) * (ratio - 1)
        day["extra"] += ratio - 1
    return days


def day_factors(bars: pl.DataFrame, actions: pl.DataFrame) -> list[DayFactor]:
    """每个除权日一个因子，按日期升序。`bars` 含 `date`/`close`，顺序由本函数负责。"""
    _refuse_unpriceable(actions)
    ordered = bars.sort("date")
    prev_closes = _prev_close_by_date(ordered)
    out: list[DayFactor] = []
    for ex_date, day in sorted(group_by_ex_date(actions).items()):
        multiplier = 1.0 + day["extra"]
        prev_close = prev_closes.get(ex_date)
        if multiplier <= 0:
            raise NotAdjustable(
                f"{ex_date} 的股数乘数是 {multiplier}：除权后的股数不可能是零或负数，"
                "这条公司行为记录自相矛盾"
            )
        if day["cash"] or day["paid"]:
            # 现金行为必须用价格折算，缺前一根 K 线就推不出参考价——给 1.0 等于
            # 宣称「这一天没有除权」，而这一天明明有
            if prev_close is None:
                raise NotAdjustable(
                    f"{ex_date} 有现金分红/配股，但库里没有它前一根 K 线，参考价无从折算"
                )
            ex_ref = ex_reference(
                prev_close, cash=day["cash"], paid=day["paid"], share_multiplier=multiplier
            )
            if ex_ref <= 0:
                raise NotAdjustable(
                    f"{ex_date} 的除权参考价算出非正值 {ex_ref}：每股派息加配股价不可能"
                    "等于或超过前收，这条记录不能进因子"
                )
            out.append(
                DayFactor(
                    ex_date=ex_date, prev_close=prev_close, ex_ref=ex_ref,
                    factor=prev_close / ex_ref,
                )
            )
        else:
            # 纯股数变化与前收无关，缺前一根 K 线也推得动。因子恒等于乘数：
            # 10 送 5 之后的价格要乘 1.5 才和除权前连得上（1 手变 1.5 手，钱没多）
            ex_ref = prev_close / multiplier if prev_close is not None else None
            out.append(
                DayFactor(
                    ex_date=ex_date, prev_close=prev_close, ex_ref=ex_ref,
                    factor=multiplier,
                )
            )
    return out


def hfq_frame(bars: pl.DataFrame, actions: pl.DataFrame) -> pl.DataFrame:
    """加两列：`adj_factor`（截至当日的累乘）与 `close_hfq`。

    累乘口径是「除权日 ≤ t」，所以除权**当天**的 K 线已经带上当天的因子——
    当天的价格本来就是除权后的，不加会在当天留下一个假跳水。
    """
    ordered = bars.sort("date")
    factors = day_factors(ordered, actions)
    cumulative: list[float] = []
    running = 1.0
    cursor = 0
    for day in ordered["date"].to_list():
        while cursor < len(factors) and factors[cursor].ex_date <= day:
            running *= factors[cursor].factor
            cursor += 1
        cumulative.append(running)
    adj = pl.Series("adj_factor", cumulative)
    return ordered.with_columns(adj, (pl.col("close") * adj).alias("close_hfq"))


def _refuse_unpriceable(actions: pl.DataFrame) -> None:
    """D39：源报了「有这么件事」但折不出数 ⇒ 整段拒绝出数，而不是当它没发生。

    这是港美股唯一那道闸：D37 的涨跌停证据规则只对 A 股生效（HK/US 没有涨跌幅上限可依据），
    所以一条被静默丢弃的实物分派在图上不会留下任何痕迹——它只会让跨那天的涨跌差几个点，
    而几个点看起来完全像正常波动。宁可不出数。
    """
    if "kind" not in actions.columns:
        return
    blocked = actions.filter(pl.col("kind") == "unknown").sort("ex_date")
    if blocked.is_empty():
        return
    first = blocked.row(0, named=True)
    extra = f"等 {blocked.height} 条" if blocked.height > 1 else ""
    raise NotAdjustable(
        f"{first['ex_date']} 有一条无法折算的公司行为"
        f"（{first.get('note') or '源未给原文'}{extra}）：推不出因子，这一段的复权涨跌不能给"
    )


def _prev_close_by_date(bars: pl.DataFrame) -> dict[date, float]:
    """每个交易日 → 上一根 K 线的收盘价。

    用游标而不是 self-join：join 会把「这个日期没有前一根」变成列里的 null，
    而这里要的语义是「这个键不存在」，两者在下游是同一种错法的两种写法。
    """
    dates = bars["date"].to_list()
    closes = bars["close"].to_list()
    return {day: closes[i - 1] for i, day in enumerate(dates) if i > 0}
