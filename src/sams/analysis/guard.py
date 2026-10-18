"""复权出数前的三道闸（D36 / D37 / D39 的调用侧）。

这个文件存在的理由只有一条：**守卫必须站在出数的路上，而不是站在某个调用方的习惯里**。
如果只有 `interval_return` 检查覆盖与未标记跳变，那么下一个想要「一条后复权序列」的人
（M2-B 的指标引擎）调 `hfq_frame` 或自己拿 `day_factors` 累乘，就绕过了全部检查——症状不是
报错，是一条看起来完全正常的错曲线，正是 U12 要防的那一类。所以判定放这里，
`adjust.adjusted_series` 是它唯一的出口，任何新调用方走进来的第一步就是过闸；
`tests/test_layering.py::SOLE_CONSUMER` 把这组裸原语钉成「只有 `analysis/adjust.py` 能用」，
钉不住的是「有人自己写一遍乘法」——那一条留给 M2-B：指标入口只收 `AdjustedSeries` 这个类型，
不收 `pl.DataFrame`，让裸序列在签名层面就递不进去。

三道闸各挡一种说谎：

* **覆盖（D36）**：库里 0 行既可能是「从没分过红」也可能是「没查过」。没有确认记录就没有
  资格说「这段区间没有除权」。
* **证据（D37）**：A 股裸价出现超过该板块最宽涨跌幅的跳变，而这一天的跳变又对不上任何
  已记录的行为 ⇒ 一定有漏记的除权 ⇒ 即使覆盖标记说确认过也拒绝。
* **收盘 Sanity**：收盘价为空就不是 K 线，除权参考价无从折算。缺数要说不出口，不是崩栈。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from typing import TYPE_CHECKING, Protocol

import polars as pl

from sams.core.codes import daily_price_limit
from sams.core.errors import NotAdjustable

if TYPE_CHECKING:
    from sams.analysis.adjust import DayFactor

#: 上市初期不设涨跌幅的日子：A 股注册制新股前 5 个交易日、主板首日 44%/36%。
#: 用**日历日**而不是「第几根 K 线」划这条线，因为守卫拿到的 `bars` 常常只是请求窗口——
#: 按窗口里的位置算，一只 2007 年上市的票只要这次取了最近六天行情，窗口开头那个 −30%
#: 就会被当成「上市首日」放过去。10 个日历日足够覆盖五个交易日（含一个周末）；
#: 长假期间会误免几天，那是安全的一侧：多放过的是一天正常波动，不是漏掉一次除权。
_IPO_GRACE_DAYS = 10


def _is_ipo_period(day: date, listed_on: date) -> bool:
    return listed_on <= day <= listed_on + timedelta(days=_IPO_GRACE_DAYS)


class ConfirmedSpan(Protocol):
    """`storage.repos.corp_actions_coverage.CoverageRecord` 的读法视图。

    这里用 Protocol 而不是直接 import 那个类：分析层不该依赖仓库层的具体类型，
    而「一段被确认过的区间」这件事本身只有四个字段。
    """

    covered_from: date
    covered_to: date
    full_history: bool
    src: str


def is_covered(
    spans: Sequence[ConfirmedSpan], *, start: date, end: date
) -> ConfirmedSpan | None:
    """这个区间的除权记录是否被某个源完整确认过（D49：只问窗口，不问今天）。

    全历史确认只看上界：源说「这只票的历史我给全了，截至 covered_to」，那么
    covered_to 之前的每一天都有答案，包括它派息之前的年份。

    这里**不再**要求 `covered_to >= today`。那条规则的出发点是「确认做于过去某天，
    它对 covered_to 之后没有主张」，这个理由是对的，但它管的是 covered_to 之后的日子，
    管不到一个早已结束的窗口：2024 年 7 月那段不会因为今天没人重新问过源就长出新的除权日。
    把它做成读取层的拒绝闸，实际效果是每天第一次查历史区间都得先联网重采集，采集失败就打
    「拒绝出数」——那句话看起来像数据有问题，而它真正说的是「今天没人去问过」。
    「什么时候该重新问源」因此归同步调度（M3/M4），不归这里；作为交换，放行时把
    `covered_to` 一起交出去，让读数的人自己看得见这条陈述有多新。
    """
    for span in spans:
        if span.full_history:
            if span.covered_to >= end:
                return span
        elif span.covered_from <= start and span.covered_to >= end:
            return span
    return None


def require_coverage(
    market: str,
    code: str,
    start: date,
    end: date,
    spans: Sequence[ConfirmedSpan],
) -> ConfirmedSpan:
    """没有确认记录就拒绝出数，返回放行那一条（源名 + 它做到哪天为止）。

    拒绝文案把「为什么不能直接算」写全：裸价区间涨跌在「没查过」那种情况下是一个错数，
    而调用方此刻正打算把它当成收益写进快照。
    """
    span = is_covered(spans, start=start, end=end)
    if span is None:
        raise NotAdjustable(
            f"{market}/{code} 的 {start}~{end} 没有公司行为覆盖确认："
            "`corp_actions` 里 0 行既可能是这只票从没分过红，也可能是没查过它，"
            "裸价区间涨跌在第二种情况下是错数。先跑 `sams sync actions --market "
            f"{market} --code {code}`"
        )
    return span


def require_real_closes(market: str, code: str, bars: pl.DataFrame) -> pl.DataFrame:
    """收盘价必须每根都有：缺一个数，那一天的除权参考价就无从折算，而下游只会拿到
    一串 NaN 而不是拒绝。"""
    if "close" not in bars.columns:
        raise NotAdjustable(f"{market}/{code} 的 K 线帧没有 close 列，复权无从下手")
    # null 的 `is_finite()` 本身是 null，取反还是 null，`filter` 会把坏行当成不匹配放过去——
    # 缺数就是这么漏进来的，所以空值要单独点名列出来
    bad = bars.filter(pl.col("close").is_null() | ~pl.col("close").is_finite())["date"].to_list()
    if bad:
        raise NotAdjustable(
            f"{market}/{code} 有 {len(bad)} 根 K 线缺收盘价（最早 {bad[0]}）："
            "除权参考价要按前一根收盘折算，缺数就不能给复权结果"
        )
    return bars.sort("date")


def reject_unmarked_jumps(
    market: str,
    code: str,
    bars: pl.DataFrame,
    factors: Sequence[DayFactor],
    *,
    listed_on: date | None = None,
) -> None:
    """D37：超过该板块最宽涨跌幅、又对不上任何已记录行为的裸价跳变 = 一定有漏记的除权。

    「对得上」按价格而不是按涨跌率判：除权当天股价照样能涨跌停，所以真实收盘落在
    参考价 ±涨跌幅 之内都算被这一天的记录解释了。10 送 10 是一次 −50% 的跳变，它是
    U12 里最该被算对的场景，绝不能被这道闸当成「没记录的除权」拒掉——那等于守卫把自己
    存在的理由否定了。

    `listed_on` 缺失时**不给上市豁免**：宁可多拒一次上市首日的暴涨，也不能让调用方忘记传
    参就把 −30% 的不可能跳变放出去。
    """
    limit = daily_price_limit(market, code)
    if limit is None:
        return
    closes = bars["close"].to_list()
    dates = bars["date"].to_list()
    explained = {f.ex_date: f for f in factors if f.prev_close and f.ex_ref}
    for i in range(1, len(closes)):
        if listed_on is not None and _is_ipo_period(dates[i], listed_on):
            continue
        prev, today = closes[i - 1], closes[i]
        if not prev:
            continue
        move = today / prev - 1
        # 放宽 1.5 倍：涨跌停价是按 0.01 元取整的，低价股的实际涨幅会略超名义比例
        if abs(move) <= limit * 1.5:
            continue
        recorded = explained.get(dates[i])
        if recorded is not None and abs(today - recorded.ex_ref) <= recorded.ex_ref * limit:
            continue
        hint = (
            "；若这几天正是上市初期（上市日起 "
            f"{_IPO_GRACE_DAYS} 个日历日内），用 `sams analysis return --listed-on <上市日>` "
            "显式豁免——那条命令有这个参数，这里也给得出下一步"
            if listed_on is None
            else ""
        )
        raise NotAdjustable(
            f"{market}/{code} 在 {dates[i]} 裸价变动 {move:+.1%}，超过该板块涨跌幅上限 "
            f"{limit:.0%} 的 1.5 倍，且对不上这一天记录在案的公司行为："
            "一定有未记录的公司行为，区间涨跌不能按裸价给" + hint
        )
