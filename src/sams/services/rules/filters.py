"""四个过滤开关（spec §10.1）。纯函数，判不出来就放行。

引擎（T-14）的次序是：模板判中 ⇒ 过一遍这里 ⇒ 冷却 ⇒ 投递。过滤器在冷却之前，
因为「这条本来就不该推」与「这条被半小时内的上一条压了」是两件事，
投递记录里那一句必须说得出是哪一种。

`Gate` 是引擎备好的「这一趟之外的世界」：现在的时刻、那个市场的时段、
该市场最近一个交易日、那只票上未处理的那条告警。这四样都不在 `Snapshot` 里——
`Snapshot` 是**那个对象**的事实，`Gate` 是**这一次求值**的环境。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sams.core.errors import ValidationError
from sams.core.rules import (
    FILTER_KEYS,
    FILTER_MINUTES,
    STALE_PRICE_DAYS,
    Template,
    Verdict,
    escalates,
)
from sams.core.timeutil import Session
from sams.services.rules.snapshot import Snapshot


@dataclass(frozen=True)
class OpenAlert:
    """那只票上一条**未处理**的告警（`no_open_alert` 的输入）。

    只带三个字段：过滤器要的是「有没有、那一档多响」，而正文与通道属于 `sams alert show`。
    """

    id: str
    stage: str
    on: date


@dataclass(frozen=True)
class Gate:
    now: datetime | None = None
    sessions: tuple[Session, ...] = ()
    #: 该市场最近一个已开市的日子（`CalendarService` 给）。None ⇒ 日历没读到
    last_session_on: date | None = None
    open_alert: OpenAlert | None = None


def time_window(snapshot: Snapshot, gate: Gate, verdict: Verdict, built: Template) -> str:
    """开盘前后各 5 分钟内不推价格类：集合竞价那个价不是成交出来的价（§10.1）。

    向两侧各伸 5 分钟而不是只卡开盘之后：A 股 9:15–9:25 是竞价，未匹配的大额买卖会在
    那十分钟里造出一个假破位。窗口外的时段（午后、收盘后）一概放行。
    """
    if not built.price_class or gate.now is None or not gate.sessions:
        return ""
    window = timedelta(minutes=FILTER_MINUTES)
    now = gate.now
    for session in gate.sessions:
        if session.open - window <= now < session.open + window:
            return (
                f"{session.name} 开盘前后 {FILTER_MINUTES} 分钟内不推价格类："
                f"开盘 {session.open:%H:%M}，现在 {now:%H:%M}，"
                "那几分钟里的价不是连续竞价的价"
            )
    return ""


def data_freshness(snapshot: Snapshot, gate: Gate, verdict: Verdict, built: Template) -> str:
    """行情太旧 ⇒ 不推。界是 `STALE_PRICE_DAYS`，判据是 `>`（3 天正好过）。"""
    if not built.price_class:
        return ""
    if snapshot.price is None:
        return f"{snapshot.market}/{snapshot.code} 没有过闸现价：价格类条件这一趟不推"
    age = snapshot.price_age_days
    if age is None:
        return ""  # 不知道多旧 ⇒ 放行（取向见模块开头）
    if age > STALE_PRICE_DAYS:
        return (
            f"现价是 {age} 天前的读数（{snapshot.price_on}），超过 {STALE_PRICE_DAYS} 天："
            "拿它报「跌破止损」是在报一个已经过去的价格"
        )
    return ""


def exclude_halt(snapshot: Snapshot, gate: Gate, verdict: Verdict, built: Template) -> str:
    """当天没有行 ⇒ 不推。见上面那枚测试：这是证据而不是权威数据。"""
    if not built.price_class or gate.last_session_on is None:
        return ""
    if snapshot.price_on is not None and snapshot.price_on < gate.last_session_on:
        return (
            f"该市场最近一个交易日是 {gate.last_session_on}，而 "
            f"{snapshot.market}/{snapshot.code} 最后一根日线在 {snapshot.price_on}："
            "那天没有行（停牌、未上市或没同步到），价格类推送在这里没有意义"
        )
    return ""


def no_open_alert(snapshot: Snapshot, gate: Gate, verdict: Verdict, built: Template) -> str:
    """同票已有一条未处理告警时不重复推——除非这一条更响（与 D94 同一个理由）。"""
    open_alert = gate.open_alert
    if open_alert is None:
        return ""
    try:
        louder = escalates(open_alert.stage, verdict.stage)
    except ValidationError:
        # `alerts.stage` 那一列没有词表约束（选股入池那类历史行写的是别的词），认不出
        # 「上一档有多响」就是判不出来，而这里的取向是判不出来⇒放行。
        # 让它抛出去等于让一条脏历史把整趟求值打掉。
        return ""
    if louder:
        return ""
    return (
        f"已有一条未处理的 {open_alert.stage} 告警（{open_alert.id}，"
        f"{open_alert.on}）在这一只票上；这一条 {verdict.stage} 不比它更响 ⇒ "
        "先处理那一条（`sams alert handle`）"
    )


FILTERS: dict[str, Callable[[Snapshot, Gate, Verdict, Template], str]] = {
    "time_window": time_window,
    "data_freshness": data_freshness,
    "exclude_halt": exclude_halt,
    "no_open_alert": no_open_alert,
}


def run_filters(
    filters: Mapping[str, object],
    *,
    snapshot: Snapshot,
    gate: Gate,
    verdict: Verdict,
    built: Template,
) -> str:
    """按 `FILTER_KEYS` 的顺序过一遍**被打开**的开关，第一个命中的那句返回；全过 ⇒ 空串。

    顺序走 `FILTER_KEYS` 而不是 `filters` 的键序：库里那串 JSON 的键序是历史（用户改过
    两次开关，最后一次写进去的顺序谁都没在意），而压制理由要能复算。
    """
    for key in FILTER_KEYS:
        if not bool(filters.get(key)):
            continue
        reason = FILTERS[key](snapshot, gate, verdict, built)
        if reason:
            return reason
    return ""
