"""证伪台账的词表：指标、方向、状态，各只有一份（D232 P0-3）。

下拉框、入库校验、页面上的中文名都从这里取。比较方向也只在这里实现一次：
`holds()` 是「这条假设成立吗」的唯一答案，服务层不另写一个 if。
"""

from __future__ import annotations

from sams.core.errors import ValidationError

OPEN = "open"
MET = "met"
REFUTED = "refuted"
UNKNOWN = "unknown"

#: 指标 → 中文名。单位见 `PERCENT_METRICS`：比率类按小数口径存（0.15 = 15%，D58）。
METRICS: dict[str, str] = {
    "roe": "净资产收益率",
    "revenue_yoy": "营收同比",
    "profit_yoy": "净利同比",
    "dividend_yield": "股息率",
    "pe_ttm": "市盈率 TTM",
    "pb": "市净率",
    "pr": "市赚率",
}

#: 比率类：0.15 要读成 15%，而 PE/PB/PR 是倍数。
PERCENT_METRICS = frozenset({"roe", "revenue_yoy", "profit_yoy", "dividend_yield"})

OPS: dict[str, str] = {">=": "≥", "<=": "≤", ">": ">", "<": "<"}

STATUSES: dict[str, str] = {
    OPEN: "待验证",
    MET: "成立",
    REFUTED: "已证伪",
    UNKNOWN: "数据不足",
}


def metric_label(metric: str) -> str:
    if metric not in METRICS:
        raise ValidationError(
            f"没有叫 {metric!r} 的指标：能用的是 {'、'.join(METRICS)}"
        )
    return METRICS[metric]


def op_label(op: str) -> str:
    if op not in OPS:
        raise ValidationError(f"没有叫 {op!r} 的比较方向：能用的是 {' '.join(OPS)}")
    return OPS[op]


def status_label(status: str) -> str:
    return STATUSES.get(status, status)


def format_value(metric: str, value: float | None) -> str:
    """读数与阈值共用一种排法：比率折百分数，倍数两位小数，空值是一个「—」。"""
    if value is None:
        return "—"
    if metric in PERCENT_METRICS:
        return f"{value * 100:.2f}%"
    return f"{value:.2f}"


def holds(op: str, value: float, threshold: float) -> bool:
    """这条假设在这一次读数下成立吗。方向不认得的当场拒，不猜。"""
    op_label(op)
    if op == ">=":
        return value >= threshold
    if op == "<=":
        return value <= threshold
    if op == ">":
        return value > threshold
    return value < threshold
