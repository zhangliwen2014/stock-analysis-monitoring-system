"""区间与赔率（M2-D，spec §8.3）：把 PR 与分位换成四个价位和一个赔率。

这一层只有两条规矩，都写在形状上：

1. **每一个数都能手算**。合理区间是 `现价 × 目标 PR ÷ 现在的 PR`，止损是
   `现价 × (1-p)` 或 `现价 - k×ATR`，赔率是 `(目标 - 现价) ÷ (现价 - 止损)` ——
   屏幕上写的那条式子就是这里算的那一条，拿计算器能对上（黄金用例见测试）。
2. **缺哪一路就拒哪一项，绝不顶替**。ATR 止损掉回 8% 止损，屏幕上两个数长得一模一样，
   只有把它跟周均线放在一起时才知道它不是——所以每种止损只吃自己的那个输入。

`Entry` 比 `Level` 多一个 `ready`：买入区是一段**条件**（现价到没到），不是又一个价。
「合理下限 60、现价 30」读起来像矛盾，其实是「比下限还便宜」，那句话由 evidence 来说。

胜率不在这里（唯一出处是 `scoring.Composite`，M5 才回测）：两处各说一句「未回测」就可以
不一致，而它们都会出现在同一屏上。
"""

from __future__ import annotations

from dataclasses import dataclass

from sams.analysis.valuation.percentile import Percentile
from sams.analysis.valuation.styles import ENTRY_RULES, STOP_KINDS, Style
from sams.core.errors import ValidationError
from sams.core.fundamentals import BASIS_PB_ESTIMATE, BASIS_PE, BASIS_REFUSED

#: PR 的两条分支（D59）在区间这一侧的口径说明：合理区间继承 PR 自己的口径。
_BASIS_NOTES = {
    BASIS_PE: "口径：PE ÷ ROE ÷ 100，ROE 是已披露的那一期",
    BASIS_PB_ESTIMATE: "口径：PB ÷ ROE ÷ ROE ÷ 100，含预估 ROE——合理区间继承这个预估",
}
_BASIS_KEYS = (BASIS_PE, BASIS_PB_ESTIMATE, BASIS_REFUSED)


@dataclass(frozen=True)
class Level:
    """一个价位（`low == high`）或一段区间，出门带着算它的式子和拒它的原因。"""

    key: str
    low: float | None
    high: float | None
    evidence: str
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.low is not None and self.high is not None


@dataclass(frozen=True)
class Entry(Level):
    """买入区：`ready` 是「现价到没到这一段」，None ⇒ 这一段压根没算出来。"""

    ready: bool | None = None


@dataclass(frozen=True)
class Odds:
    value: float | None
    evidence: str
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.value is not None


@dataclass(frozen=True)
class Plan:
    """一条可读的估值结论：区间、买入区、止损、目标、赔率，各带自己的那句话。"""

    style: str
    price: float | None
    fair: Level
    entry: Entry
    stop: Level
    target: Level
    odds: Odds
    #: 依附项自己的拒绝原因（比如分位读数没过样本地板），屏幕上要单独成行
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return (
            self.fair.ok
            and self.entry.ok
            and self.stop.ok
            and self.target.ok
            and self.odds.ok
        )

    @property
    def refusals(self) -> tuple[str, ...]:
        """缺多少路输入，屏幕上就该有多少句「这一项为什么没有」。"""
        lines = [part.reason for part in (self.fair, self.entry, self.stop, self.target, self.odds)]
        return tuple(line for line in lines + list(self.notes) if line)

    @property
    def evidence(self) -> str:
        parts = [self.fair, self.entry, self.stop, self.target]
        return "\n".join(
            [part.evidence for part in parts if part.evidence] + [self.odds.evidence]
        )


def _fmt(value: float | None) -> str:
    return "无" if value is None else f"{value:g}"


def _refused(key: str, formula: str, why: str) -> Level:
    """拒了的价位也带着自己那条式子出门：屏幕上不能只剩一句「没有」。"""
    return Level(key, None, None, formula, why)


def _refused_entry(formula: str, why: str) -> Entry:
    return Entry("entry", None, None, formula, why)


def fair_band(
    *,
    price: float | None,
    pr: float | None,
    fair_pr_low: float,
    fair_pr_high: float,
    basis: str | None = None,
) -> Level:
    """合理区间：`现价 ÷ 现在的 PR × 目标 PR`。

    这条式子的意思是「盈利不变、市场肯为一块钱利润多付到目标 PR 时，价该在哪」——
    所以 PR 缺失或不大于 0 时它没有分母，只能拒（D59 那句「PR 永不为 0」在这里生效）。
    """
    if basis is not None and basis not in _BASIS_KEYS:
        raise ValidationError(
            f"PR 口径只认 {' / '.join(_BASIS_KEYS)}，收到 {basis!r}："
            "口径写错的话，合理区间会带着一个不存在的分母出门"
        )
    formula = "合理区间（fair）：现价 ÷ 现在的 PR × 目标 PR"
    if price is None or price <= 0:
        return _refused("fair", formula, f"现价没有数或不为正（{_fmt(price)}）：式子的左边就空了")
    if pr is None or pr <= 0 or basis in (None, BASIS_REFUSED):
        return Level(
            "fair",
            None,
            None,
            formula,
            f"PR 没有数或不为正（{_fmt(pr)}，口径 {basis}）：它就是这条式子的分母，"
            "拿 `sams analysis fundamentals` 看这一只的 PR 为什么算不出",
        )
    low = price * fair_pr_low / pr
    high = price * fair_pr_high / pr
    return Level(
        "fair",
        min(low, high),
        max(low, high),
        f"{formula}：现价 {price:g} ÷ 现在的 PR {pr:g} × 目标 PR "
        f"{fair_pr_low:g}~{fair_pr_high:g} ⇒ {min(low, high):g}~{max(low, high):g}"
        f"（{_BASIS_NOTES[basis]}）",
    )


def _ordered_band(raw_low: float, raw_high: float) -> tuple[float, float]:
    """归一区间两端。

    两端来自 `± 带宽`，谁在上谁在下本来是定的；还排一次是因为带宽一旦写成负数，
    屏幕上那个「上限」会变成下限，而 `ready` 跟着说谎。
    """
    low, high = sorted((raw_low, raw_high))
    return low, high


def entry_zone(
    *,
    rule: str,
    price: float | None,
    fair_low: float | None,
    weekly_ma20: float | None,
    pr_percentile: float | None,
    entry_band_pct: float,
    percentile_entry_max: float,
) -> Entry:
    """买入区：价值型等的是回到合理下限，趋势型等的是回踩周均线不破。

    两条规则问的是两件不同的事，所以「到没到」的比较也不一样：前者是「现价 ≤ 区间上限」
    （更便宜当然也算到），后者是「现价落在区间里」（跌穿就不是回踩而是破位）。
    """
    if rule == "near_fair_low":
        return _entry_near_fair_low(
            price=price,
            fair_low=fair_low,
            pr_percentile=pr_percentile,
            entry_band_pct=entry_band_pct,
            percentile_entry_max=percentile_entry_max,
        )
    if rule == "weekly_ma20_pullback":
        return _entry_pullback(price=price, weekly_ma20=weekly_ma20, entry_band_pct=entry_band_pct)
    raise ValidationError(
        f"买入规则只认 {' / '.join(ENTRY_RULES)}，收到 {rule!r}："
        "新规则要在 `styles.ENTRY_RULES` 里登记，否则模板会声明一个这里算不出的东西"
    )


def _entry_near_fair_low(
    *,
    price: float | None,
    fair_low: float | None,
    pr_percentile: float | None,
    entry_band_pct: float,
    percentile_entry_max: float,
) -> Entry:
    formula = "买入区（near_fair_low）：合理下限 ± 带宽，且历史分位过闸门"
    if price is None or price <= 0:
        return _refused_entry(
            formula, f"现价没有数或不为正（{_fmt(price)}）：没人可跟区间比"
        )
    if fair_low is None:
        return _refused_entry(formula, "合理下限没有数（合理区间没算出来），买入区依附于它")
    if pr_percentile is None:
        return Entry(
            "entry",
            None,
            None,
            formula,
            f"没有历史分位读数，闸门（< {percentile_entry_max:g}%）无法判断："
            "先算 PR 分位（`pr_percentile`）",
        )
    if pr_percentile >= percentile_entry_max:
        return Entry(
            "entry",
            None,
            None,
            formula,
            f"分位 {pr_percentile:g}% 不在「便宜的那一档」里（闸门 {percentile_entry_max:g}%）："
            "这一档的入场条件没到，不是给出一个买入区让你追",
        )
    low, high = _ordered_band(
        fair_low * (1.0 - entry_band_pct), fair_low * (1.0 + entry_band_pct)
    )
    ready = price <= high
    tail = (
        f"现价 {price:g} 已在买入区上限 {high:g} 之下 ⇒ 条件已到"
        if ready
        else f"现价 {price:g} 还在买入区上限 {high:g} 之上"
    )
    under = (
        f"（现价低于合理下限 {low:g}：这是「比下限还便宜」，不是等跌到 {low:g} 才买）"
        if price < low
        else ""
    )
    return Entry(
        "entry",
        low,
        high,
        f"{formula}：合理下限 {fair_low:g} ± {entry_band_pct:.0%} ⇒ {low:g}~{high:g}；"
        f"分位 {pr_percentile:g}% 通过闸门 {percentile_entry_max:g}%；{tail}{under}",
        ""
        if ready
        else f"现价 {price:g} 还在买入区上限 {high:g} 之上：这一档要等回落到 {high:g} 以下",
        ready,
    )


def _entry_pullback(
    *, price: float | None, weekly_ma20: float | None, entry_band_pct: float
) -> Entry:
    formula = "买入区（weekly_ma20_pullback）：回踩周MA20 不破"
    if price is None or price <= 0:
        return _refused_entry(
            formula, f"现价没有数或不为正（{_fmt(price)}）：没人可跟区间比"
        )
    if weekly_ma20 is None or weekly_ma20 <= 0:
        return _refused_entry(
            formula, f"周MA20 没有数或不为正（{_fmt(weekly_ma20)}）：这一档贴着它算"
        )
    low, high = _ordered_band(weekly_ma20 * (1.0 - entry_band_pct), weekly_ma20)
    ready = low <= price <= high
    if ready:
        reason = ""
        tail = f"现价 {price:g} 落在回踩区里 ⇒ 条件已到"
    elif price > high:
        reason = f"现价 {price:g} 还悬在周MA20 {high:g} 上方：这一档等的是回踩，不追高"
        tail = reason
    else:
        reason = (
            f"现价 {price:g} 已跌破回踩区下沿 {low:g}（周MA20 {high:g}）："
            "回踩「不破」才是这条规则，跌穿它不是便宜，是信号变了"
        )
        tail = reason
    return Entry(
        "entry",
        low,
        high,
        f"{formula}：周MA20 {high:g} − 带宽 {entry_band_pct:.0%} ⇒ {low:g}~{high:g}；{tail}",
        reason,
        ready,
    )


def stop_level(
    *,
    kind: str,
    price: float | None,
    stop_pct: float | None = None,
    atr_multiplier: float | None = None,
    fixed: float | None = None,
    atr: float | None = None,
    weekly_ma20: float | None = None,
) -> Level:
    """止损位：四种算法各吃各的输入，缺了就拒，不掉到「另一种也能算」上。"""
    if kind not in STOP_KINDS:
        raise ValidationError(
            f"止损种类只认 {' / '.join(STOP_KINDS)}，收到 {kind!r}："
            "M3 的规则模板还有五种，那些是触发器不是这里的参数"
        )
    if price is None or price <= 0:
        return _refused(
            "stop", f"止损（{kind}）", f"现价没有数或不为正（{_fmt(price)}）：止损按它算"
        )
    if kind == "fixed":
        if fixed is None:
            return _refused(
                "stop", "止损（fixed）", "止损种类是 fixed，但没给定价：固定要有人给那个数"
            )
        value = fixed
        evidence = f"止损（fixed）：给定的价 {value:g}（来源是人给的建仓/离场位，不是这里算的）"
    elif kind == "pct":
        if stop_pct is None or stop_pct <= 0:
            return _refused("stop", "止损（pct）", f"止损百分比没有数或不为正（{_fmt(stop_pct)}）")
        value = price * (1.0 - stop_pct)
        evidence = f"止损（pct）：现价 {price:g} × (1 - {stop_pct:.0%}) = {value:g}"
    elif kind == "atr2":
        if atr is None or atr <= 0 or atr_multiplier is None or atr_multiplier <= 0:
            return _refused(
                "stop",
                "止损（atr2）",
                f"ATR（{_fmt(atr)}）或倍数（{_fmt(atr_multiplier)}）没有数或不为正：这一种要两根数都齐",
            )
        value = price - atr_multiplier * atr
        evidence = f"止损（atr2）：现价 {price:g} - {atr_multiplier:g}×ATR {atr:g} = {value:g}"
    else:  # weekly_ma20
        if weekly_ma20 is None or weekly_ma20 <= 0:
            return _refused(
                "stop", "止损（weekly_ma20）", f"周MA20 没有数或不为正（{_fmt(weekly_ma20)}）"
            )
        value = weekly_ma20
        evidence = f"止损（weekly_ma20）：离场位=周MA20 {value:g}（收盘跌破即走，现价 {price:g}）"
    if value <= 0:
        return _refused(
            "stop",
            f"止损（{kind}）",
            f"算出来是 {value:g}：止损位不为正，那是这几个数凑不出离场位，不是「止损在 0」",
        )
    return Level("stop", value, value, evidence)


def pick_target(
    *,
    price: float | None,
    fair_high: float | None,
    recent_high: float | None,
    lookback_days: int,
) -> Level:
    """目标位：合理上限与前高，**取离现价更近的那个**（spec §8.3）。

    取近不是保守，是不让「PR 0.4 ⇒ 合理上限 3 倍现价」那种数独自变成止盈价：
    前高是市场真的付过的价，它先把这条腿剪短。
    """
    formula = "目标（target）：合理上限与前高二者取近"
    if price is None or price <= 0:
        return _refused(
            "target", formula, f"现价没有数或不为正（{_fmt(price)}）：没有「之上」可言"
        )
    fair_label = "合理上限"
    recent_label = f"过去 {lookback_days} 日高点"
    pool = [
        (label, value)
        for label, value in ((fair_label, fair_high), (recent_label, recent_high))
        if value is not None and value > price
    ]
    if not pool:
        return Level(
            "target",
            None,
            None,
            formula,
            f"现价 {price:g} 不比合理上限 {_fmt(fair_high)} 与前高 {_fmt(recent_high)} 低："
            "上涨空间没有参照物，这一行给不出赔率",
        )
    label, value = min(pool, key=lambda item: item[1])
    missing = ""
    if fair_high is None:
        missing = f"合理上限没有数（合理区间没算出来），只有前高 {_fmt(recent_high)}；"
    elif recent_high is None:
        missing = f"过去 {lookback_days} 日无前高（窗口内没成交），只用合理上限 {_fmt(fair_high)}；"
    shown = "、".join(f"{item[0]} {item[1]:g}" for item in pool)
    return Level(
        "target",
        value,
        value,
        f"{formula}：{missing}现价 {price:g} 之上的候选 {shown} ⇒ 取更近的 {label} {value:g}",
    )


def reward_risk(*, price: float | None, stop: float | None, target: float | None) -> Odds:
    """`赔率 = (目标 - 现价) ÷ (现价 - 止损)`：spec §8.3 原式，一步减法都不藏。"""
    formula = "赔率：(目标 - 现价) ÷ (现价 - 止损)"
    if price is None or stop is None or target is None:
        missing = [
            name
            for name, value in (("现价", price), ("止损", stop), ("目标", target))
            if value is None
        ]
        return Odds(None, formula, f"赔率的式子缺 {'、'.join(missing)}：少一个数就不是同一个赔率")
    risk = price - stop
    if risk <= 0:
        return Odds(
            None,
            formula,
            f"现价 {price:g} 已经在止损 {stop:g} 上或下方：分母不大于 0，"
            "那不是无穷大的赔率，是这笔交易已经不成立",
        )
    value = (target - price) / risk
    return Odds(
        value,
        f"{formula}：(目标 {target:g} - 现价 {price:g}) ÷ "
        f"(现价 {price:g} - 止损 {stop:g}) = {value:g}",
    )


def trade_plan(
    *,
    style: Style,
    price: float | None,
    pr: float | None = None,
    basis: str | None = None,
    pr_percentile: Percentile | None = None,
    weekly_ma20: float | None = None,
    atr: float | None = None,
    recent_high: float | None = None,
    fixed_stop: float | None = None,
) -> Plan:
    """把一只票的读数换成一条结论；参数一律从模板取，不在这里再写一遍字面量。"""
    if pr_percentile is not None and pr_percentile.metric != "pr":
        raise ValidationError(
            f"入场闸门问的是 PR 的历史分位，收到 metric={pr_percentile.metric!r} 的分位："
            "把 PE 分位喂进 PR 的闸门，屏幕上那句「便宜的那一档」就是在说别的东西"
        )
    fair = fair_band(
        price=price,
        pr=pr,
        fair_pr_low=style.param("fair_pr_low"),
        fair_pr_high=style.param("fair_pr_high"),
        basis=basis,
    )
    entry = entry_zone(
        rule=style.param("entry_rule"),
        price=price,
        fair_low=fair.low,
        weekly_ma20=weekly_ma20,
        pr_percentile=pr_percentile.value if pr_percentile is not None else None,
        entry_band_pct=style.param("entry_band_pct"),
        percentile_entry_max=style.param("percentile_entry_max"),
    )
    stop = stop_level(
        kind=style.param("stop_kind"),
        price=price,
        stop_pct=style.param("stop_pct"),
        atr_multiplier=style.param("atr_multiplier"),
        fixed=fixed_stop,
        atr=atr,
        weekly_ma20=weekly_ma20,
    )
    target = pick_target(
        price=price,
        fair_high=fair.high,
        recent_high=recent_high,
        lookback_days=style.param("target_lookback_days"),
    )
    odds = reward_risk(price=price, stop=stop.low, target=target.low)
    notes: tuple[str, ...] = ()
    if pr_percentile is not None and not pr_percentile.ok and pr_percentile.reason:
        notes = (pr_percentile.reason,)
    return Plan(
        style=style.name,
        price=price,
        fair=fair,
        entry=entry,
        stop=stop,
        target=target,
        odds=odds,
        notes=notes,
    )
