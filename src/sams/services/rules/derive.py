"""§9.2 建仓派生：一条论 → 六条规则，加那张「改了参数回写哪一列」的表。

派生出来的规则**都挂在论上**（`target_type='thesis'`、`thesis_id` 非空）。这一个形状
决定了两件事：`RuleRepo.remove` 会拒删它们（§9.2「不可删除，只能停用」），而 `write_back`
只在这种规则上生效——用户自己建的一条 `price.cross_below` 里那个数是他心里的价，
不是他论点上的区间上沿，回写会把一条与他当前论点无关的东西写进他的论点。

只有两条规则抄了论上的数字（止损线、买区上沿），所以回写表只有两项。剩下四条读的是
论本身（`tp.tiered` / `thesis.odds_below` / `thesis.drift` 走 `Ask.thesis`，
`scheduled.reevaluate` 走披露日），改了论不必重派它们。

派生不写 `thesis_events`：`to_status` 非空，而派生不是一次状态跳转（`set_levels` 的注释
同一个理由）。它写的只有 `rules` 那几行：`RuleRepo.create` 不留修订史，`rule_revisions`
的第一份快照要等这条规则第一次被实质修改才有。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from sams.core.rules import template_for
from sams.core.version import VersionGate
from sams.storage.repos.rules import Rule, RuleRepo
from sams.storage.repos.theses import Thesis, ThesisRepo

#: §9.2 那一行点名的默认阈值：赔率跌破它，这笔就值得重看一遍
ODDS_FLOOR = 1.5
#: 披露日之后几天内提醒复检。这个数不是这里定的：`core/rules.py` 里 `within_days`
#: 那个参数写着 default=5.0（单位「天」= 自然日），派生取的就是它
REEVAL_DAYS = 5
#: 分批止盈的两档：到半程与到目标（§10.2「分批提醒」的那个「分批」）。
#: 派生给的是串的形状，库里存下来读回的是校验过的列表——两种形状 `update` 都吃得下
TIERS = "0.5,1.0"

#: 改了这条规则的哪个参数 ⇒ 回写论上的哪一列。参数名列在表里，是因为它不全局统一：
#: `stop.fixed` 读 `price`，而 `stop.pct` / `stop.trailing` 那两种的键是 `pct`——
#: 只在这一处写死一列名的话，加一行就会静默不回写
WRITE_BACK = {
    "stop.fixed": ("stop_price", "price"),
    "price.cross_below": ("entry_high", "price"),
}

#: 派生这一条时默认带的过滤器：披露日窗口内每个交易日都会各判中一次，而 §10.1 的降噪
#: 取向是「同票已有一条没点掉的就不再推」。`templates.scheduled_reevaluate` 的 docstring
#: 承诺的就是这一行——不带它，那一段窗口天天响。
REEVAL_FILTERS = {"no_open_alert": True}

#: 只有这一条真的与账户有关（`tp.tiered` 要读那一笔的持仓成本），所以幂等键里只有它
#: 带账户；其余五条是论的事实，按账户复制就会让同一件事按账户发 N 条重复告警
PER_ACCOUNT = frozenset({"tp.tiered"})


@dataclass(frozen=True)
class Blueprint:
    """一条该建的规则（还没进库）。`reason` 是屏幕上那句「凭什么建这条」。"""

    kind: str
    name: str
    params: dict
    reason: str
    filters: dict = field(default_factory=dict)


@dataclass(frozen=True)
class NotDerived:
    """这一条这次不建，并且说得出为什么——沉默的跳过就是「规则怎么少了两条」。"""

    kind: str
    reason: str


@dataclass(frozen=True)
class DeriveReport:
    """一次派生的回执：建了哪几条、哪些已存在、哪些这次不建（附理由）。给 T-17 的维护面
    与 T-18 的 `sams rule derive` 打印用，不是只给测试看的。"""

    thesis_id: str
    created: tuple[Rule, ...] = ()
    already: tuple[str, ...] = ()
    skipped: tuple[NotDerived, ...] = ()


def blueprints_for(
    thesis: Thesis, *, account_id: str | None = None
) -> tuple[Blueprint | NotDerived, ...]:
    """论上的哪一列派生出哪条规则，一次列全。缺列就是一条 `NotDerived`，不是静默少一条。"""
    name = thesis.code
    out: list[Blueprint | NotDerived] = []
    if thesis.entry_high is None:
        out.append(NotDerived("price.cross_below", "论上没有区间上沿：没有可盯的那条线"))
    else:
        out.append(
            Blueprint(
                "price.cross_below",
                f"{name} 跌进买入区间",
                {"price": thesis.entry_high},
                f"区间上沿 {thesis.entry_high:g}：从上方跌进来就是可执行计划",
            )
        )
    if thesis.stop_price is None:
        out.append(NotDerived("stop.fixed", "论上没有止损价：三级提醒没有那条线"))
    else:
        out.append(
            Blueprint(
                "stop.fixed",
                f"{name} 止损三级",
                {"price": thesis.stop_price},
                f"止损价 {thesis.stop_price:g}：pre / hit / urgent 三档由模板给",
            )
        )
    if thesis.target_price is None:
        out.append(NotDerived("tp.tiered", "论上没有目标价：分档没有终点"))
    elif account_id is None:
        out.append(NotDerived("tp.tiered", "没有账户：分批止盈要读这一笔的成本"))
    else:
        out.append(
            Blueprint(
                "tp.tiered",
                f"{name} 分批止盈",
                {"tiers": TIERS},
                f"目标价 {thesis.target_price:g}：到半程与到终点各提醒一次",
            )
        )
    if thesis.reward_risk is None:
        out.append(NotDerived("thesis.odds_below", "论上没有赔率：这条永远不会成立"))
    else:
        out.append(
            Blueprint(
                "thesis.odds_below",
                f"{name} 赔率跌破阈值",
                {"threshold": ODDS_FLOOR},
                f"现赔率 {thesis.reward_risk:g}：跌破 {ODDS_FLOOR:g} 就值得重看这笔",
            )
        )
    out.append(
        Blueprint(
            "scheduled.reevaluate",
            f"{name} 披露日复检",
            {"within_days": REEVAL_DAYS},
            "新一期财报落地后，论上的数就是旧数",
            filters=dict(REEVAL_FILTERS),
        )
    )
    out.append(
        Blueprint(
            "thesis.drift",
            f"{name} 观点漂移",
            {},
            "周复检掉一档就提醒（§9.3：只提示，不替谁决定平仓）",
        )
    )
    return tuple(out)


def carries_price(kind: str) -> bool:
    """这一种的参数里有没有一个以「元」计价的数（0017 的日期戳只管它们）。

    派生出来的那六条里只有 `price.cross_below` 与 `stop.fixed` 带绝对价；`tiers`、`threshold`、
    `within_days` 是这一份文件给的常量，给它们盖一个「论上那天填的」的戳，说的是一个
    不指任何价位的日期。手填那条路不走这里，它盖的本来就是「人刚刚敲过」的那一天。
    """
    return bool(template_for(kind).money_params())


class RuleDeriver:
    """把蓝图落到库里。幂等：同一 (论, kind) 已存在就不再建——**跳过而不是覆盖**，
    用户改过的那条参数不能在一次重复派生里被悄悄写回蓝图值。

    幂等键上只有 `tp.tiered` 多带一个 `account_id`（`PER_ACCOUNT`）：同一只论在两个账户
    各有一笔、成本不同，那一档要各算一次；其余五条按 kind 幂等，重复派生不会按账户
    复制出 N 条同样的事实。
    """

    def __init__(
        self,
        rules: RuleRepo,
        theses: ThesisRepo,
        *,
        versions: VersionGate | None = None,
    ) -> None:
        self.rules = rules
        self.theses = theses
        #: 盘中缓存的失效闸（§5.4）；可选，理由见 `RuleService.versions`
        self.versions = versions

    def derive(
        self, user_id: str, thesis_id: str, *, account_id: str | None = None
    ) -> DeriveReport:
        thesis = self.theses.require(user_id, thesis_id)
        have = {(row.kind, row.account_id) for row in self.rules.by_thesis(thesis_id)}
        created: list[Rule] = []
        already: list[str] = []
        skipped: list[NotDerived] = []
        for item in blueprints_for(thesis, account_id=account_id):
            if isinstance(item, NotDerived):
                skipped.append(item)
                continue
            owner = account_id if item.kind in PER_ACCOUNT else None
            if (item.kind, owner) in have:
                already.append(item.kind)
                continue
            created.append(
                self.rules.create(
                    user_id,
                    kind=item.kind,
                    name=item.name,
                    target_type="thesis",
                    target_ref=thesis_id,
                    params=item.params,
                    filters=item.filters,
                    account_id=owner,
                    thesis_id=thesis_id,
                    # 抄自论上那几列的规则，锚是那一次读数的那一天（0013 的 `levels_on`）；
                    # 手填的价位没有测量日，跟着留 None 而不是盖今天
                    params_on=thesis.levels_on if carries_price(item.kind) else None,
                )
            )
            have.add((item.kind, owner))
        if created and self.versions is not None:
            # 只有真建了规则才 bump：第二趟全是 already，库没变，不该把缓存全清
            self.versions.bump("rules")
        return DeriveReport(
            thesis_id=thesis_id,
            created=tuple(created),
            already=tuple(already),
            skipped=tuple(skipped),
        )


def write_back(
    rule: Rule, before: Mapping[str, object], after: Mapping[str, object]
) -> dict[str, float]:
    """编辑一条派生规则时要回写论上的哪几列。空字典 = 不动任何论点（两种情况都常见）。"""
    if rule.thesis_id is None:
        return {}
    entry = WRITE_BACK.get(rule.kind)
    if entry is None:
        return {}
    column, param_key = entry
    value = after.get(param_key)
    if value is None or before.get(param_key) == value:
        return {}
    return {column: float(value)}
