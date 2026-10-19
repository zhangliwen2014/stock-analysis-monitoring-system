"""历史分位：「现在这个数，处在它自己过去 N 年的第几档」（M2-D，spec §8.3）。

`PR ≤ 0.4` 只回答「便宜」，不回答「便宜到什么程度」。分位把单个数读成一个位置，代价是
它继承了三条已经定过的口径，每一条写错的症状都是**一个看起来完全正常的数**：

- **D61 / D9**：某一天能用的 ROE，是那天的**发布日**已经到期的那一期，而且「哪一期」的
  选法必须与 `storage/repos/financials.py::load_as_of` 那条 SQL 一致（按报告期倒序）。
  两处不一致时，`sams analysis fundamentals` 与这条分位曲线会在同一只票上说两个数。
- **D59**：PE 那一支与 PB 那一支算出来的 PR 不可比。历史里只有已披露 ROE、没有「预估」，
  所以这一整段只走 PE 那一支；PE 不为正的那些天被丢掉并计数——把它们混进来，那条曲线
  混的是两种分母，排出来的名次没有意义。
- **D66**：一个漏了 ÷100 的 ROE 会让它配上的每一天都便宜两个数量级。它不是「那一天偏低」，
  而是那一天、以及同一个分母覆盖的整段日子一起偏低，会压低整条曲线的读数。所以口径可疑
  的分母逐日丢弃，不取中位数、不夹逼。

窗口两端随数一起出门（`window_from` / `window_to`）：「过去五年」是一个可复算的日期区间，
不是一个形容词。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta

from sams.analysis.fundamentals import pr
from sams.core.errors import ValidationError
from sams.core.fundamentals import (
    BASIS_PE,
    ROE_BASIS,
    ROE_BASIS_ANNUAL,
    is_year_end,
)

#: 分位要的最小可比天数。约等于一个交易日年（A 股 244 天、美股 252 天）：少于这个数时
#: 分位说的是抽样噪声而不是估值位置——一个 30 天样本的「10 分位」只是「这三十天里最低的那天」。
#: 写成常量并由拒绝那句话引用：地板换一个数，屏幕上那句解释必须跟着换。
MIN_SAMPLES = 250

#: 「过去 N 年」的默认 N。spec §8.3 的例子读作「过去五年」，所以默认就是五年，
#: 而不是「库里有多少年算多少年」——后者会让同一只票在同步多了两年数据后换个分位。
DEFAULT_WINDOW_YEARS = 5

#: 一年按 365 天算窗口。日历日而不是交易日：调用方给的是按日估值行，
#: 停牌的那些天本来就没有行，用交易日切窗口会把「五年」切成五个日历年加一截。
_DAYS_PER_YEAR = 365

#: 倍数那一族可以做「自己的历史分位」：不比分母，只比自己。
_MULTIPLE_METRICS = ("pe_ttm", "pb")


@dataclass(frozen=True)
class ValuationDay:
    """`valuation_daily` 里的一行，只留分位要用的那三列。"""

    day: date
    pe_ttm: float | None
    pb: float | None = None


@dataclass(frozen=True)
class DisclosedPeriod:
    """`financials` 里的一行。`published_date` 为 None 是「不知道它哪天公开」，不是「没公开」。"""

    report_date: date
    roe: float | None
    published_date: date | None


@dataclass(frozen=True)
class Percentile:
    """一次分位读数。`value` 为 None 时 `reason` 一定非空——没有「算不出而不知道为什么」。"""

    #: 0~100 的位置；None ⇒ 这个数没有可比历史，不出数
    value: float | None
    #: 参与比较的那个「现在」的值（PR / PE / PB 本身）
    current: float | None
    #: 早于 `current_day` 且在窗口内的可比天数（今天的自己不投票，见模块下那条用例）
    samples: int
    metric: str
    window_from: date
    window_to: date
    #: 窗口内被丢掉的行，按原因分三堆。三堆加起来 + samples + 今天 = 窗口内的行数
    skipped_no_pe: int = 0
    skipped_no_roe: int = 0
    skipped_caliber: int = 0
    #: 财务行里那些「有报告期而没有发布日」的期数：它们永远不参与，但必须被数到（D61）
    unpublished_periods: int = 0
    #: 今天那一行用的是哪一期年报 / 最新一期（跟着数走，读的人能验算）
    roe_period: date | None = None
    reason: str = ""
    evidence: str = ""
    #: 今天那一行实际是哪一天：`as_of` 常常比库里最新的估值行晚
    current_day: date | None = None

    @property
    def ok(self) -> bool:
        return self.value is not None


def _pit_roe(
    periods: Sequence[DisclosedPeriod], day: date, *, basis: str
) -> tuple[float | None, date | None]:
    """那一天已经公开了的、该口径下最新的那一期 ROE。

    「最新」按**报告期**判，与仓库那条 SQL 同一个 ORDER BY——理由写在 `load_as_of` 的
    docstring 里：追溯重述不覆盖更新的那一期，否则两条命令会对同一天说两个数。
    """
    best: DisclosedPeriod | None = None
    for row in periods:
        if row.published_date is None or row.published_date > day:
            continue
        if row.roe is None:
            continue
        if basis == ROE_BASIS_ANNUAL and not is_year_end(row.report_date):
            continue
        if best is None or row.report_date > best.report_date:
            best = row
    if best is None:
        return None, None
    return best.roe, best.report_date


def _unpublished_count(periods: Sequence[DisclosedPeriod], *, basis: str) -> int:
    """该口径下「有报告期而没有发布日」的期数（与 `load_as_of` 的 `dropped` 同口径）。"""
    return sum(
        1
        for row in periods
        if row.published_date is None
        and (basis != ROE_BASIS_ANNUAL or is_year_end(row.report_date))
    )


def _rank(values: Sequence[float], current: float) -> float:
    """分位 = 「不高于现在的天数 ÷ 可比天数」。

    等值算「不高于」：一条 PE 全程不变的曲线读作 100 而不是 50。这不是笔误——把等值算进
    分子后，「现在的值 = 历史最小值」与「现在的值 = 历史最大值」分别落在 0 与 100 两端，
    而 `>= 50` 那种「取中位」的写法会在平直历史上报出 0，把一条没动过的曲线说成跌到谷底。
    """
    at_or_below = sum(1 for value in values if value <= current)
    return 100.0 * at_or_below / len(values)


def _pr_on(
    day: ValuationDay, periods: Sequence[DisclosedPeriod], *, basis: str
) -> tuple[float | None, str]:
    """返回 (值, 丢掉它的原因)；原因是空串时值一定可用。"""
    roe, _ = _pit_roe(periods, day.day, basis=basis)
    if day.pe_ttm is None or day.pe_ttm <= 0 or not math.isfinite(day.pe_ttm):
        # D59：这一支要的是「预估 ROE」，而历史里没有预估这回事。缺 PE 也一样：没有分子
        return None, "no_pe"
    if roe is None or roe <= 0:
        return None, "no_roe"
    result = pr(pe_ttm=day.pe_ttm, roe=roe)
    if not result.ok or result.basis != BASIS_PE:
        # 剩下的两种：分母落进口径死区（D66），或那个比值溢出成非有限值
        return None, "caliber"
    return result.value, ""


def _tally(
    *, no_pe: int, no_roe: int, caliber: int, unpublished: int
) -> str:
    """被丢掉的行按原因说出口——一句都不许省，否则样本数与实际排的东西不是同一件事。"""
    parts = []
    if no_pe:
        parts.append(f"PE 缺失或不为正 {no_pe} 天（那一支要预估 ROE，历史里没有）")
    if no_roe:
        parts.append(f"当天还没有已披露且为正的 ROE {no_roe} 天")
    if caliber:
        parts.append(f"ROE 口径可疑或比值溢出 {caliber} 天")
    if unpublished:
        parts.append(f"财务行里 {unpublished} 期因为没有发布日而全程未被采信")
    return "；丢掉：" + "；".join(parts) if parts else ""


def _too_thin(samples: int) -> str:
    """样本不够时的那句话，两种分位共用一个措辞（地板也共用一个数）。"""
    return (
        f"窗口内只有 {samples} 个可比交易日，少于地板 {MIN_SAMPLES} 天："
        "这个分位说的是抽样噪声，不是估值位置"
    )


def _refuse(base: Percentile, why: str) -> Percentile:
    """拒绝出门的那条：`why` 说清为什么不出数，「丢掉了多少行」跟着 base 自己的计数器走。"""
    dropped = _tally(
        no_pe=base.skipped_no_pe,
        no_roe=base.skipped_no_roe,
        caliber=base.skipped_caliber,
        unpublished=base.unpublished_periods,
    )
    return replace(base, reason=f"{why}{dropped}")


def pr_percentile(
    rows: Sequence[ValuationDay],
    periods: Sequence[DisclosedPeriod],
    as_of: date,
    *,
    basis: str = ROE_BASIS_ANNUAL,
    window_years: int = DEFAULT_WINDOW_YEARS,
) -> Percentile:
    """PR 的历史分位：逐日「当天的 PE(TTM) ÷ 当天已披露的那一期 ROE」。

    `as_of` 必填（D54 那一族的规矩：没有「默认今天」这条路），它同时是窗口的右端。
    """
    if basis not in ROE_BASIS:
        raise ValidationError(
            f"ROE 口径只认 {' 或 '.join(ROE_BASIS)}（年报 / 最新一期），收到 {basis!r}"
        )
    window_from = as_of - timedelta(days=_DAYS_PER_YEAR * window_years)
    in_window = sorted(
        (row for row in rows if window_from < row.day <= as_of), key=lambda row: row.day
    )
    unpublished = _unpublished_count(periods, basis=basis)
    empty = Percentile(None, None, 0, "pr", window_from, as_of)
    if not in_window:
        return _refuse(
            replace(empty, unpublished_periods=unpublished),
            f"窗口 {window_from}~{as_of} 内没有任何按日估值行："
            "先同步这只票的估值（`sams sync valuation`）",
        )

    today = in_window[-1]
    current, today_why = _pr_on(today, periods, basis=basis)
    buckets = {"no_pe": 0, "no_roe": 0, "caliber": 0}
    values: list[float] = []
    for row in in_window[:-1]:
        value, why = _pr_on(row, periods, basis=basis)
        if value is None:
            buckets[why] += 1
        else:
            values.append(value)
    if today_why:
        # 今天这一行也在窗口里被数到，只是不投票：拒绝时说的那句「丢掉几天」才与行数对得上
        buckets[today_why] += 1

    roe_period = _pit_roe(periods, today.day, basis=basis)[1] if current is not None else None
    evidence = (
        f"valuation_daily.pe_ttm × financials.roe（PIT：发布日 ≤ 当日）逐日配对，"
        f"窗口 {window_from}~{as_of}（{(as_of - window_from).days} 天），"
        f"可比样本 {len(values)} 天，现在这一天 {today.day}"
    ) + _tally(
        no_pe=buckets["no_pe"],
        no_roe=buckets["no_roe"],
        caliber=buckets["caliber"],
        unpublished=unpublished,
    )
    base = replace(
        empty,
        current=current,
        samples=len(values),
        skipped_no_pe=buckets["no_pe"],
        skipped_no_roe=buckets["no_roe"],
        skipped_caliber=buckets["caliber"],
        unpublished_periods=unpublished,
        roe_period=roe_period,
        current_day=today.day,
        evidence=evidence,
    )

    if current is None:
        return _refuse(
            base,
            f"现在这一天（{today.day}）在 PE 那一支上算不出 PR："
            "没有可比的历史位置可说（跨支不可比，D59）",
        )
    if len(values) < MIN_SAMPLES:
        return _refuse(base, _too_thin(len(values)))
    return replace(base, value=_rank(values, current))


def metric_percentile(
    rows: Sequence[ValuationDay],
    metric: str,
    as_of: date,
    *,
    window_years: int = DEFAULT_WINDOW_YEARS,
) -> Percentile:
    """PE / PB 自己的历史分位（spec §8.3 的「PE-PB 历史分位」）：不比分母，只比自己。

    它不需要财务行，所以一只票在没有 ROE 的时候仍然可以回答「现在的 PE 在它自己
    过去五年里排第几」——而这句话与 PR 的分位不是同一件事，`metric` 字段带着这个区别。
    """
    if metric not in _MULTIPLE_METRICS:
        raise ValidationError(
            f"倍数分位只认 {' / '.join(_MULTIPLE_METRICS)}，收到 {metric!r}："
            "比率列的历史在 `financials` 那一侧（按报告期），不在这里"
        )
    window_from = as_of - timedelta(days=_DAYS_PER_YEAR * window_years)
    pairs = sorted(
        ((row.day, getattr(row, metric)) for row in rows if window_from < row.day <= as_of),
        key=lambda item: item[0],
    )
    usable = [(day, value) for day, value in pairs if value is not None and value > 0]
    dropped = len(pairs) - len(usable)
    # 丢掉的那几行只写进 evidence：`skipped_*` 那三个计数器是 PR 那条链的三堆原因，
    # 拿 `skipped_no_pe` 装「pb 缺失」会让同一个字段名在两种 metric 下说两种话
    if not usable:
        return Percentile(
            None,
            None,
            0,
            metric,
            window_from,
            as_of,
            reason=(
                f"窗口 {window_from}~{as_of} 内没有可用的 {metric}"
                f"（{len(pairs)} 行里 {dropped} 行缺失或不为正）："
                "先同步估值数据（`sams sync valuation`）"
            ),
        )
    current_day, current = usable[-1]
    values = [value for _, value in usable[:-1]]
    evidence = (
        f"valuation_daily.{metric} 逐日序列（不配财务行），"
        f"窗口 {window_from}~{as_of}（{(as_of - window_from).days} 天），"
        f"可比样本 {len(values)} 天，现在这一天 {current_day}"
    )
    if dropped:
        evidence += f"；丢掉 {dropped} 天（缺失或不为正：那一天的倍数不是「便宜」，是没有意义）"
    base = Percentile(
        None,
        current,
        len(values),
        metric,
        window_from,
        as_of,
        current_day=current_day,
        evidence=evidence,
    )
    if len(values) < MIN_SAMPLES:
        return replace(base, reason=_too_thin(len(values)))
    return replace(base, value=_rank(values, current))
