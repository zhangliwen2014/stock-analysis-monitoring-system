"""把人写下来的价搬进今天的价空间（spec §10.2 的强制验收）。

`_fold.py` 那件外套管的是**机器算出来的**那些数：MA、ATR、周线、持仓期最高价，它们出自
后复权序列，读取层已经把它们折回现价口径了。这一枚管另一头——人填的那几个：`stop.fixed`
里那条线、这一笔的成本、论上那条止损线与那个目标价。它们是某一天在**那一天的裸价坐标系**
里写下的数，中间只要有一次除权，今天的裸价就换了坐标系。10 派 1、前收 10.00 的那一天之后，
「10 元」站在 9.00；仍然拿 10.00 去比 9.30，就是在指挥一个人去卖一笔并没有亏的仓。

算式是 D34 那个公式的另一面：因子说「今天的价要乘多少才连得上昨天」，所以昨天写下的一个价
要**除以**它之后每一个除权日的因子（`_fold.shift`）。券商在除权日调整成本价是同一个动作。

三条口径，改这一段代码之前先读完：

1. **搬不动就不许半搬，而后果按「这一条拿不拿它做判断」分两种。** 少搬一条线让止损提前响，
   而屏幕上那句「跌破止损」会被当命令执行；不响只是慢一步。所以拿它做判断（模板里声明在
   `Template.judged`）⇒ 整条不判；只是显示那一列（`stop_line`）⇒ 注明原值可能偏高、规则照判。
   每一句都自带下一步（D55），而且下一步必须**真能修好这一种缺口**：「不知道哪天填的」与
   「本地 K 线起点太晚」是两个修法。
2. **库里这段没有除权记录时一个数都不动**（`factors` 为空那条快路径）。任何按锚缺失做的
   拒绝在没有除权时也生效的话，0017 之前建的那些行会全部变成没有结论。
3. **一个数只按它自己那一天的锚搬一次。** 本次合成的那些数（`valuation.plan.*`）的锚就是
   本趟那一天，`steps_after` 给回空 ⇒ 一个因子也不除。所以「已经在今价空间里的数」不需要
   一份例外名单——名单要有人记得加，而漏掉的那一条就是一个没人怀疑的误报。

这一枚不读库、不发通知、不碰时钟：三种错法都能在 `Snapshot` 上直接复现。
`Snapshot` 只在注解里出现，所以两边不成环。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import TYPE_CHECKING

from sams.core.rules import Template
from sams.services._fold import shift, steps_after
from sams.services.rules.templates import money
from sams.storage.repos.rules import Rule

if TYPE_CHECKING:
    from sams.services.rules.snapshot import Snapshot


@dataclass(frozen=True)
class Moved:
    """一笔搬运：`line` 是它那一屏的正文，`snapshot.moved` 是这一趟的清单。

    `on`（它原本属于哪天）与 `ex_dates`（跨了哪几天）不是装饰：一个动了的数如果不说清
    「凭什么动」，读的人只能选择信或不信（D54 里裸价与复权并列给是同一个理由）。
    """

    #: 是谁的那个价（「止损价」/「成本」/「论上那条止损线」/「论上那个目标价」）
    label: str
    #: 落在哪个字段上：`params.price` / `avg_cost` / `stop_line` / `target_price`
    column: str
    on: date
    before: float
    after: float
    ex_dates: tuple[date, ...] = ()
    #: 币种后缀：这些数一律带币种出门（§5.4 规则 3），空串时 `money` 会写「元」
    currency: str = ""

    @property
    def line(self) -> str:
        """那一屏的一行正文。"""
        days = "、".join(str(day) for day in self.ex_dates)
        return (
            f"换算：{self.label} {money(self.before, self.currency)}（{self.on} 写下）"
            f"→ {money(self.after, self.currency)}，跨 {len(self.ex_dates)} 个除权日（{days}）"
        )


@dataclass(frozen=True)
class Relocation:
    """搬完的那两份输入，加上「哪些数搬不动，因此这一条不判」。

    返回两份（快照与参数）而不是就地改：`rule.params` 是库里那一份，就地改会让人在
    `sams rule list` 上看到的与这一趟判用的不是同一个数，而屏幕上那行「换算」正是拿两边
    对照的。快照同理——`RuleData.read` 出来的那一份在同一趟里会被多个对象复用。
    """

    snapshot: Snapshot
    params: dict
    #: 每一条都是一个「不判」的理由（口径 1）
    blocked: tuple[str, ...] = ()

    @property
    def moves(self) -> tuple[Moved, ...]:
        return self.snapshot.moved


def move_prices(rule: Rule, built: Template, snapshot: Snapshot) -> Relocation:
    """一次纯搬运。参数的日期戳怎么盖章见 0017；快照那几列入参归取数处（`RuleData.read`）填。"""
    params = dict(rule.params)
    steps = snapshot.factors
    if not steps:
        return Relocation(snapshot=snapshot, params=params, blocked=())
    floor = snapshot.bars_from
    if floor is None:
        return Relocation(
            snapshot=snapshot,
            params=params,
            blocked=("读不到这段 K 线从哪天开始，除权表因此无法确认完整：这一趟不判",),
        )
    currency = snapshot.currency
    # 补行情这句话在几处都要用，而它带的参数只有一个来源
    sync = f"sams sync daily --market {snapshot.market} --code {snapshot.code}"
    moved: list[Moved] = []
    annotated: list[str] = []
    blocked: list[str] = []
    changes: dict[str, object] = {}

    def carry(
        value: float, *, on: date | None, label: str, column: str, refill: str
    ) -> tuple[float, str, str]:
        """搬一个数 →（给回的值，搬不动的原因，修它的那句命令）。

        原因为空串 ⇒ 搬成了，或者这个数与那段除权无关（它的锚之后没有除权日）。
        「修它的那句命令」按原因的两种写法各指各的：不知道哪天填的 ⇒ 重新填一次，
        填的那一天在库里第一根 K 线之前 ⇒ 补行情。
        """
        if on is None:
            return value, f"{label} 不知道是哪天写下的（那一行没有日期戳）", refill
        if on < floor:
            return (
                value,
                f"{label} 属于 {on}，比库里第一根 K 线（{floor}）还早，中间那段的除权没记进来",
                f"`{sync}` 把 K 线补到 {on}",
            )
        crossing = steps_after(steps, after=on)
        if not crossing:
            return value, "", ""
        arrived = shift(value, crossing)
        moved.append(
            Moved(
                label=label,
                column=column,
                on=on,
                before=value,
                after=arrived,
                ex_dates=tuple(item.ex_date for item in crossing),
                currency=currency,
            )
        )
        return arrived, "", ""

    def consequence(reason: str, fix: str, *, judged: bool) -> str:
        """那两种「搬不动」的后果各是哪句话（口径 1）。"""
        if judged:
            return f"{reason}：搬它会少搬，这一条不判。{fix}"
        return f"{reason}：搬它会少搬，屏幕上那一列按原值显示，可能偏高。{fix}"

    for param in built.money_params():
        raw = params.get(param.name)
        if raw is None:
            # 「这一行压根没有这个键」不是这里的话：`_verdict` 那张 `KeyError` 网就是为
            # 老模板写下的那一行准备的，屏幕上那一句已经写着去哪个命令补。在这里再报一遍
            # 是两件外套，而两处「搬不动」里只有一处带着正确的下一步
            continue
        # 参数是人自己填的阈值，所以「搬不动」在这里只有不判这一种后果
        arrived, reason, fix = carry(
            float(raw),
            on=rule.params_on,
            label=param.label,
            column=f"params.{param.name}",
            refill="跑一次 `sams rule edit` 重填它，日期戳就盖上了",
        )
        if reason:
            blocked.append(consequence(reason, fix, judged=True))
        else:
            params[param.name] = arrived

    for column, label, on, refill in (
        (
            "avg_cost",
            "成本",
            snapshot.opened_on,
            "`sams ledger buy --on 成交日` 重新记那一笔，建仓日就回来了",
        ),
        (
            "stop_line",
            "论上那条止损线",
            snapshot.stop_on,
            "`sams thesis revise` 不带 `--stop` 重算一次，那次读数就有日期了（0013）",
        ),
        (
            "target_price",
            "论上那个目标价",
            snapshot.target_on,
            "`sams thesis revise` 不带 `--target` 重算一次，那次读数就有日期了（0013）",
        ),
    ):
        value = getattr(snapshot, column)
        if value is None:
            continue
        judged = column in built.judged
        arrived, reason, fix = carry(
            float(value), on=on, label=label, column=column, refill=refill
        )
        if reason:
            sentence = consequence(reason, fix, judged=judged)
            (blocked if judged else annotated).append(sentence)
        else:
            changes[column] = arrived

    if moved or annotated:
        snapshot = replace(
            snapshot, moved=tuple(moved), missing=snapshot.missing + tuple(annotated), **changes
        )
    return Relocation(snapshot=snapshot, params=params, blocked=tuple(blocked))
