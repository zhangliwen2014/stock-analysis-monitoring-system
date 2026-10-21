"""每个 kind 一个纯函数：给一份 `Snapshot` 与那几条参数，判一次（spec §10.3）。

`Outcome` 而不是 `Verdict | None`：**「没触发」与「判不了」是两句话**。前者不该有任何
输出，后者必须给出那句话——否则一条因为缺数据而从没响过的规则，在屏幕上与一条
工作正常的规则长得一模一样（§1 那张表的最后一行）。

所有阈值都在**现价口径 + 标的币种**里比（§5.4 规则 3）：折回因子是 T-6 乘好的，
这里再乘一次就是两件外套，除权日会只有一侧折回。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sams.core.rules import HIT, PRE, URGENT, Verdict
from sams.services.rules.snapshot import Snapshot

#: 「快到了」那一档有多近：现价离止损线不到 3% 就报 `pre`（§10.2 点名的那个刻度）。
#: 写成一处而不是每个模板的参数：九种止损各配一个 `pre_band`，用户要回答一个他自己
#: 也说不清的数，而配出九个值就是没有值。
PRE_BAND = 0.03


@dataclass(frozen=True)
class Ask:
    """一次求值的输入：谁的哪个对象、此刻的数、这条规则的参数。"""

    snapshot: Snapshot
    params: Mapping[str, object]
    #: 这条规则挂着的那条论（若有）。模板读它的价位与状态时**只**从这里读，不自己去查库：
    #: `Snapshot` 已经查过一次，两次之间规则还能被改
    thesis: object | None = None


@dataclass(frozen=True)
class Outcome:
    """一个模板的一次输出。`hit` 为 None 时 `why_not` 是那句话，或者那句空。"""

    hit: Verdict | None = None
    why_not: str = ""


def money(value: float | None, currency: str) -> str:
    """阈值与现价的措辞：数后面永远跟着币种（§5.4 规则 3）。

    金额类阈值不允许不带币种，而 A 股是元、美股是美元——屏幕上写「止损 100」的那条规则，
    用户在手机上会按另一种货币去理解它。
    """
    if value is None:
        return "未知"
    return f"{value:.2f} {currency or '元'}"


def stop_stage(
    snapshot: Snapshot, line: float, *, pre_band: float = PRE_BAND
) -> str | None:
    """三档判定。顺序不能调：先看跳空，再看是否已破，最后才看「快到了」。

    `urgent` 要求**前收盘在线上方**而**开盘在线下方**——只有这种形状才是「一开盘就没机会」。
    只看开盘的话，一条早就破了的线每趟都会升格成 urgent，那一档的意义被用光。
    """
    price = snapshot.price
    if price is None:
        return None
    if (
        snapshot.day_open is not None
        and snapshot.prev_close is not None
        and snapshot.day_open < line
        and snapshot.prev_close >= line
    ):
        return URGENT
    if price < line:
        return HIT
    if price < line * (1.0 + pre_band):
        return PRE
    return None


def _no_price(snapshot: Snapshot) -> str:
    return (
        f"没有 {snapshot.market}/{snapshot.code} 过闸的现价：这一趟不判任何价格类条件"
        "（§10.1 的新鲜度过滤器拦掉的就是这种读数）"
    )


def _gap_note(snapshot: Snapshot, line: float, stage: str) -> str:
    """`urgent` 那句话要把「开盘 vs 前收盘」两个数一起说出来：只报现价的话，
    读的人看不出这是跳空而不是慢慢跌过去的。"""
    if stage != URGENT:
        return ""
    return (
        f"；开盘 {money(snapshot.day_open, snapshot.currency)} 直接在线 "
        f"{money(line, snapshot.currency)} 下方（前收盘 "
        f"{money(snapshot.prev_close, snapshot.currency)}），这一跳不是盘中跌过去的"
    )


def _hit(snapshot: Snapshot, line: float, stage: str, why: str) -> Outcome:
    return Outcome(
        Verdict(
            stage=stage,
            value=snapshot.price,
            threshold=line,
            value_text=money(snapshot.price, snapshot.currency),
            threshold_text=money(line, snapshot.currency),
            why=why + _gap_note(snapshot, line, stage),
        )
    )


def stop_fixed(ask: Ask) -> Outcome:
    """`stop.fixed`：跌破绝对价即报。线就是用户填的那个数（现价口径、标的币种）。"""
    snapshot = ask.snapshot
    if snapshot.price is None:
        return Outcome(why_not=_no_price(snapshot))
    line = float(ask.params["price"])
    stage = stop_stage(snapshot, line)
    if stage is None:
        return Outcome()
    return _hit(
        snapshot, line, stage, f"绝对价止损线 {money(line, snapshot.currency)}"
    )


def stop_pct(ask: Ask) -> Outcome:
    """`stop.pct`：成本下浮 x%。没有持仓就没有成本，这一条不判而不是判成 0 成本。"""
    snapshot = ask.snapshot
    if snapshot.price is None:
        return Outcome(why_not=_no_price(snapshot))
    if snapshot.avg_cost is None or snapshot.avg_cost <= 0:
        return Outcome(
            why_not=f"这一笔在账户 {snapshot.account_id} 里没有成本：`stop.pct` 的线"
            "由成本算出，缺了它就没有线"
        )
    pct = float(ask.params["pct"])
    line = snapshot.avg_cost * (1.0 - pct)
    stage = stop_stage(snapshot, line)
    if stage is None:
        return Outcome()
    return _hit(
        snapshot,
        line,
        stage,
        f"成本 {money(snapshot.avg_cost, snapshot.currency)} 下浮 {pct:.1%}"
        f" ⇒ 线 {money(line, snapshot.currency)}",
    )


def stop_trailing(ask: Ask) -> Outcome:
    """`stop.trailing`：持仓期最高价回撤 x%。最高价来自 T-6，已是现价口径。"""
    snapshot = ask.snapshot
    if snapshot.price is None:
        return Outcome(why_not=_no_price(snapshot))
    if snapshot.holding_high is None:
        return Outcome(
            why_not="没有可用的持仓期最高价（持仓不足三根日 K 时它没有意义）："
            "拿现价凑会让回撤恒为 0，那条线贴在现价上方，第一次呼吸就报止损"
        )
    pct = float(ask.params["pct"])
    line = snapshot.holding_high * (1.0 - pct)
    stage = stop_stage(snapshot, line)
    if stage is None:
        return Outcome()
    return _hit(
        snapshot,
        line,
        stage,
        f"持仓期最高价 {money(snapshot.holding_high, snapshot.currency)}"
        f" 回撤 {pct:.1%} ⇒ 线 {money(line, snapshot.currency)}",
    )


TABLE: dict[str, Callable[[Ask], Outcome]] = {
    "stop.fixed": stop_fixed,
    "stop.pct": stop_pct,
    "stop.trailing": stop_trailing,
}
