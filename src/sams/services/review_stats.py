"""§8.4 复盘口径：纯函数，输入 `AlertRepo.list_window` 的原始 dict。

拆成两半的理由：SQL 只管「把窗口里的列读回来」，分子、分母、中位数、旧版单列这些
判断全在这里——它们不需要数据库就能逐条断言（`tests/services/test_review_stats.py`），
而页面只管渲染与「—」的措辞。撤销（undo）之后的行以当前 `status` 为准：这个函数
不认识 `alert_events`，「撤销过就不算闭环」是读状态列的自动推论，不是特判。
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: 规则「忽略/触发」比的样本门槛：低于 3 条不排名（§6.4）。样本少时比率抖动大，
#: 把 1/2 排进前五是拿计数编造「规则质量」结论——那是 M5 跨期样本的事（§8.4）。
MIN_RULE_SAMPLES = 3

#: 排名取前几条（§6.4 的「最高的 5 条」）
TOP_RULES = 5

_CLOSED = ("handled", "dismissed")


@dataclass(frozen=True)
class RuleRatio:
    rule_id: str
    rule_name: str
    triggered: int
    dismissed: int
    ratio: float
    thin: bool


@dataclass(frozen=True)
class WindowStats:
    total: int
    acknowledged: int
    closed: int
    handled: int
    dismissed: int
    ack_rate: float | None
    close_rate: float | None
    act_rate: float | None
    dismiss_rate: float | None
    median_close_hours: float | None
    action_kinds: Mapping[str, int]
    dismiss_codes: Mapping[str, int]
    legacy_handled: int
    legacy_dismiss: int


def ratio(num: int, den: int) -> float | None:
    """空分母是 None，不是 0.0：「没有可关的」与「一条没关上」是两句话（§6.4）。"""
    return None if den == 0 else num / den


def summarize(rows: Sequence[Mapping[str, Any]]) -> WindowStats:
    """窗口行的分子分母。列名与 `_REVIEW_COLUMNS` 一致；时刻是带时区的 datetime。"""
    total = len(rows)
    acknowledged = sum(1 for row in rows if row["acknowledged_at"] is not None)
    handled_rows = [row for row in rows if row["status"] == "handled"]
    dismissed_rows = [row for row in rows if row["status"] == "dismissed"]
    closed = len(handled_rows) + len(dismissed_rows)
    # 旧版行：M4-B 之前关掉的没有 action_kind/dismiss_code。状态口径的比率照算
    # （它们真的被关了），两部分布的分子分母都不含它们——口径不可考，不伪造历史。
    legacy_handled = sum(1 for row in handled_rows if not row["action_kind"])
    legacy_dismiss = sum(1 for row in dismissed_rows if not row["dismiss_code"])
    action_kinds = Counter(str(row["action_kind"]) for row in handled_rows if row["action_kind"])
    dismiss_codes = Counter(
        str(row["dismiss_code"]) for row in dismissed_rows if row["dismiss_code"]
    )
    spans = [
        (row["handled_at"] - row["triggered_at"]).total_seconds() / 3600.0
        for row in rows
        if row["handled_at"] is not None and row["status"] in _CLOSED
    ]
    return WindowStats(
        total=total,
        acknowledged=acknowledged,
        closed=closed,
        handled=len(handled_rows),
        dismissed=len(dismissed_rows),
        ack_rate=ratio(acknowledged, total),
        close_rate=ratio(closed, total),
        act_rate=ratio(len(handled_rows), closed),
        dismiss_rate=ratio(len(dismissed_rows), closed),
        median_close_hours=statistics.median(spans) if spans else None,
        action_kinds=dict(action_kinds),
        dismiss_codes=dict(dismiss_codes),
        legacy_handled=legacy_handled,
        legacy_dismiss=legacy_dismiss,
    )


def top_rule_ratios(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[RuleRatio], list[RuleRatio]]:
    """(按比率降序的前 `TOP_RULES` 条, 样本不足单列)。

    排名与门槛在同一个函数里算：页面各算各的会让一条规则同时进「前五」和
    「样本不足」——门槛是排名自己的事，不是渲染的事。并列时触发多者优先，
    再按 rule_id 定序（测试要能断言顺序，不能靠 dict 的插入序）。

    `rule_id` 为 NULL 的行（SYS 系统告警，见 `monitor/queue.py` 与
    `services/scheduler/alarms.py` 的入库路径）分组前就跳过：聚成名为「None」的
    「规则」进排名、再链到 `/rules/None/edit`，是拿系统告警伪造规则质量。
    """
    by_rule: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if row["rule_id"] is None:
            continue
        by_rule.setdefault(str(row["rule_id"]), []).append(row)
    ratios = []
    for rule_id, group in by_rule.items():
        triggered = len(group)
        dismissed = sum(1 for row in group if row["status"] == "dismissed")
        ratios.append(
            RuleRatio(
                rule_id=rule_id,
                rule_name=str(group[0]["rule_name"] or ""),
                triggered=triggered,
                dismissed=dismissed,
                ratio=dismissed / triggered,
                thin=triggered < MIN_RULE_SAMPLES,
            )
        )
    ranked = sorted(
        (item for item in ratios if not item.thin),
        key=lambda item: (-item.ratio, -item.triggered, item.rule_id),
    )
    thin = sorted(
        (item for item in ratios if item.thin),
        key=lambda item: (-item.triggered, item.rule_id),
    )
    return ranked[:TOP_RULES], thin
