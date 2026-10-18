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


@dataclass(frozen=True)
class Capability:
    #: 给用户看的那三个字：日志、健康页、拒绝文案里都念它，不再各写一份
    label: str
    #: 链上缺这个能力时的那句原因（进 `FetchResult.skipped`，最终会被打印）
    lacking: str
    probe: Callable[[Any], bool]


#: 每加一种取数（基本面、资金流…）就在这里加一行：注册表、降级链、健康检查
#: 三处同时看到它，不存在「某处忘了加」这种失败模式
CAPABILITIES: dict[str, Capability] = {
    "daily_bars": Capability(label="日线", lacking="不提供日线", probe=has_bars),
    "corp_actions": Capability(label="公司行为", lacking="不提供公司行为", probe=has_actions),
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
