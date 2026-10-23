"""数据种类的单一清单：键 = 适配器上的方法名 = 注册表里 `capabilities` 里的名字。

「这个源能不能取日线」这一件事此前有三份写法：`registry.CAPABILITIES`（方法是 `daily_bars`，
能力名叫 `daily_bars`）、`sources._capabilities`（能力名叫 `bars`）、`services.sources`
按 `"bars" in entry.capabilities` 判定。加一种数据就要多改一处，而漏改的那处不会报错——
它只会把没有这个能力的源当成有，然后在采集时才「跳过」，健康页却说一切正常（D38 的原话）。
所以这里只留一份：键就是方法名，标签与「缺能力时怎么说」也挂在这一个条目上。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sams.core.errors import DatasourceError
from sams.datasource.actions import has_actions
from sams.datasource.base import has_bars
from sams.datasource.fundamentals import has_financials, has_flow, has_valuation
from sams.datasource.fx import has_fx
from sams.datasource.listed_date import has_listed_date
from sams.datasource.quote import has_quotes


@dataclass(frozen=True)
class Capability:
    #: 给用户看的那几个字：日志、健康页、拒绝文案里都念它，不再各写一份
    label: str
    #: 链上缺这个能力时的那句原因（进 `FetchResult.skipped`，最终会被打印）
    lacking: str
    probe: Callable[[Any], bool]


#: 每加一种取数就在这里加一行：注册表、降级链、健康检查三处同时看到它，
#: 不存在「某处忘了加」这种失败模式。M2-C 加的基本面三种就在下面三条。
CAPABILITIES: dict[str, Capability] = {
    "daily_bars": Capability(label="日线", lacking="不提供日线", probe=has_bars),
    "corp_actions": Capability(label="公司行为", lacking="不提供公司行为", probe=has_actions),
    "financials": Capability(label="基本面", lacking="不提供基本面", probe=has_financials),
    "valuation": Capability(label="按日估值", lacking="不提供按日估值", probe=has_valuation),
    "funding_flow": Capability(label="资金流", lacking="不提供资金流", probe=has_flow),
    # 主档的一格，不是一类数据：它不走 `_walk`（没有窗口，也不记源健康），
    # 但「哪个源能给」仍然只在这里答一次，好让 `chain_for` 与目录用同一份判据
    "listed_date": Capability(label="上市日期", lacking="不提供上市日期", probe=has_listed_date),
    # 汇率也没有「市场」这个维度（USD→CNY 与哪个市场无关），所以它不走 `chain_for`
    # 与 `_walk`，由 `registry.fetch_fx` 自己按注册顺序走一遍。登记在这张表里是为了让
    # 「谁能给汇率」与其余五种共用同一份判据：健康页、目录、降级链三处不会各说一套
    "fx_rate": Capability(label="汇率", lacking="不提供汇率", probe=has_fx),
    # 报价没有窗口，一次问一批；`chain_for(market, "quote")` 仍按这份判据过滤
    "quote": Capability(label="报价", lacking="不提供报价", probe=has_quotes),
}


def capability(method: str) -> Capability:
    try:
        return CAPABILITIES[method]
    except KeyError:
        raise DatasourceError(
            "capabilities",
            f"不认识的数据种类 {method!r}：已登记的是 {', '.join(CAPABILITIES)}",
        ) from None


def provided_by(obj: Any) -> tuple[str, ...]:
    """这个适配器（或类本身）提供哪些数据种类，按 `CAPABILITIES` 的顺序。

    探针跑在类上也能跑：`callable(getattr(cls, "daily_bars", None))` 对未绑定的函数同样为真，
    所以注册时（只有类）与路由时（有实例）共用同一份判据，不必两套代码。
    """
    return tuple(name for name, cap in CAPABILITIES.items() if cap.probe(obj))
