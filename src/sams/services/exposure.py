"""跨账户暴露与集中度（D232）：回答「这笔钱在赌什么」。

纯函数：调用方把每笔持仓折算成报告币种的市值、再给出账户现金，这里只做权重算术。
分母是 NAV（持仓市值 + 现金），不是持仓合计——否则权重和会大于 100%，现金那一部分
也从屏幕上消失。行业缺失的持仓单独归「未分类」，不并进「其他」：前者是数据没有，
后者是确实属于别的行业，两件事不能混。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

UNCLASSIFIED = "未分类"


@dataclass(frozen=True)
class HoldingValue:
    market: str
    code: str
    name: str
    currency: str
    #: 报告币种里的市值（不是盈亏）
    value: float
    #: 空串表示没有行业数据，归入「未分类」
    industry: str = ""


@dataclass(frozen=True)
class WeightedHolding:
    market: str
    code: str
    name: str
    currency: str
    industry: str
    value: float
    weight: float


@dataclass(frozen=True)
class WeightRow:
    label: str
    value: float
    weight: float


@dataclass(frozen=True)
class Exposure:
    total: float
    cash: float
    cash_weight: float
    holdings: tuple[WeightedHolding, ...]
    by_market: tuple[WeightRow, ...]
    by_currency: tuple[WeightRow, ...]
    by_industry: tuple[WeightRow, ...]
    top5_weight: float
    hhi: float
    effective_n: float
    unclassified_weight: float
    notes: tuple[str, ...] = ()


def exposure(
    holdings: list[HoldingValue],
    *,
    cash: float,
    unclassified: str = UNCLASSIFIED,
) -> Exposure:
    """权重一律对 NAV；NAV 不为正时不给权重（0 与「算不出」不是一回事，notes 说清）。

    同一只票在两个账户里是**一笔暴露**：先按 (市场, 代码) 合并再算权重，
    否则「甲户 5% + 乙户 5%」会被读成两只票，Top5 与有效只数都偏。
    """
    merged: dict[tuple[str, str], HoldingValue] = {}
    for item in holdings:
        key = (item.market, item.code)
        previous = merged.get(key)
        merged[key] = (
            replace(previous, value=previous.value + item.value)
            if previous is not None
            else item
        )
    holdings = list(merged.values())
    held = sum(item.value for item in holdings)
    total = held + cash
    if total <= 0:
        return Exposure(
            total=total,
            cash=cash,
            cash_weight=0.0,
            holdings=tuple(
                WeightedHolding(
                    item.market,
                    item.code,
                    item.name,
                    item.currency,
                    item.industry or unclassified,
                    item.value,
                    0.0,
                )
                for item in holdings
            ),
            by_market=(),
            by_currency=(),
            by_industry=(),
            top5_weight=0.0,
            hhi=0.0,
            effective_n=0.0,
            unclassified_weight=0.0,
            notes=("总资产不为正：权重算不出，先核对现金与持仓",),
        )

    weighted = sorted(
        (
            WeightedHolding(
                item.market,
                item.code,
                item.name,
                item.currency,
                item.industry or unclassified,
                item.value,
                item.value / total,
            )
            for item in holdings
        ),
        key=lambda item: item.value,
        reverse=True,
    )
    weights = [item.weight for item in weighted]
    hhi = sum(weight * weight for weight in weights)
    # 有效只数只在持仓内部归一：把现金当成很多个 0 仓位，会让「一只票 + 95% 现金」
    # 显示成有效 400 只——那个数解释不了任何事
    held_share = held / total
    held_hhi = sum((weight / held_share) ** 2 for weight in weights) if held_share > 0 else 0.0
    return Exposure(
        total=total,
        cash=cash,
        cash_weight=cash / total,
        holdings=tuple(weighted),
        by_market=_group(weighted, lambda item: item.market, total),
        by_currency=_group(weighted, lambda item: item.currency, total),
        by_industry=_group(weighted, lambda item: item.industry, total),
        top5_weight=sum(weights[:5]),
        hhi=hhi,
        effective_n=(1 / held_hhi) if held_hhi > 0 else 0.0,
        unclassified_weight=sum(
            item.weight for item in weighted if item.industry == unclassified
        ),
        notes=(),
    )


def _group(
    holdings: tuple[WeightedHolding, ...], key, total: float
) -> tuple[WeightRow, ...]:
    sums: dict[str, float] = {}
    for item in holdings:
        label = key(item)
        sums[label] = sums.get(label, 0.0) + item.value
    return tuple(
        WeightRow(label=label, value=value, weight=value / total)
        for label, value in sorted(sums.items(), key=lambda pair: pair[1], reverse=True)
    )
