"""估值合成（M2-D，spec §8.3）：把「现在的这个数」放回它自己的时间序列，再合成区间与赔率。

分层与 M2-C 一样：这里的每一个函数只吃参数、只出结论，不碰数据库。输入是 services 从
`valuation_daily` / `financials` / 序列指标里读出来的行，所以每条口径都能被逐日复算。

包内三块各管一件事，谁也不替谁下结论：

* `percentile` —— 「现在这个 PR / PE / PB 处在过去 N 年的第几档」，PIT 分位；
* `styles` —— `value` / `trend` 两套打分模板的权重与参数（可复制、可改）；
* `compose` —— 合理区间、买入区、止损、目标价与赔率的合成，以及胜率未回测时的那句空话。
"""

from __future__ import annotations

from sams.analysis.valuation.percentile import (
    DEFAULT_WINDOW_YEARS,
    MIN_SAMPLES,
    DisclosedPeriod,
    Percentile,
    ValuationDay,
    metric_percentile,
    pr_percentile,
)

__all__ = [
    "DEFAULT_WINDOW_YEARS",
    "MIN_SAMPLES",
    "DisclosedPeriod",
    "Percentile",
    "ValuationDay",
    "metric_percentile",
    "pr_percentile",
]
