"""数据源实现的注册表——「加一个源」与「用哪些源」是两件事，分别在这里和库里。

可插拔的边界刻意画在这里：
  * 本模块只管 **名字 → 实现类**。新增实现 = 在本包下放一个文件、类上打 `@source("名字")`，
    不需要改 registry、bootstrap 或 CLI 的任何一行。
  * 「哪个市场按什么顺序用哪些源、哪个源停用了」是**数据**，存在 `data_sources` /
    `data_source_routes` 两张表里，由 `services/sources.py` 维护、管理后台驱动。

配置里不允许出现任意 import 路径或模块名：那等于给运维界面开一个远程代码执行入口。
所以 `kind` 必须先在下面这张注册表里存在，才允许落库。
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sams.core.errors import DatasourceError
from sams.datasource.base import BarAdapter
from sams.datasource.capabilities import CAPABILITIES, provided_by

#: 本包里的基础设施模块，不是适配器，不参与自动发现。
#: `*_actions.py` 是纯解析件：能力（`CorpActionAdapter`）由适配器类实现，注册表按 kind
#: 建索引，同名的两个类会互相覆盖——所以一个源只能有一个适配器类，解析函数放外面。
_NOT_ADAPTERS = frozenset(
    {
        "base",
        "registry",
        "ratelimit",
        "sources",
        "calendar_providers",
        "actions",
        "capabilities",
        "minute",
        "fundamentals",
        "listed_date",
        "fx",
        "quote",
        "spot",
        "akshare_actions",
        # SP1 的两个公共件（D148）：与 listed_date/fx 一路——能力由适配器上的方法实现
        "symbol_list",
        "index_daily",
    }
)


@dataclass(frozen=True)
class SourceEntry:
    """一个已注册的实现：能服务哪些市场由实现自己声明，配置不得越权扩张。"""

    kind: str
    build: Callable[[Mapping[str, Any] | None], BarAdapter]
    markets: frozenset[str]
    #: 适配器构造函数接受的参数名；配置里的 options 只能用到这些
    options: tuple[str, ...] = ()
    note: str = ""
    module: str = ""
    #: 这个实现提供的数据种类（D38），键取自 `datasource.capabilities.CAPABILITIES`：
    #: `daily_bars` / `corp_actions`。路由按它跳过缺能力的源，管理后台按它回答
    #: 「为什么这个源在公司行为链上不发挥作用」
    capabilities: tuple[str, ...] = ()


_REGISTRY: dict[str, SourceEntry] = {}


def source(kind: str, *, options: tuple[str, ...] = (), note: str = "") -> Callable[[type], type]:
    """把适配器类登记进本表。类必须声明 `name` 与 `markets`（`BarAdapter` 契约）。

    能力（D38）由方法是否存在推出来，不让类自己再声明一遍：声明与实现会漂移，
    而漂移的方向恰好是「看起来有这个能力，调用了才知道没有」。
    """

    def decorate(cls: type) -> type:
        markets = frozenset(getattr(cls, "markets", ()))
        if not markets:
            raise DatasourceError("sources", f"{cls.__name__} 未声明 markets，无法注册为数据源")
        offered = provided_by(cls)
        if not offered:
            raise DatasourceError(
                "sources",
                f"{cls.__name__} 不提供任何一种数据（已登记的种类: "
                f"{', '.join(CAPABILITIES)}）：注册进来也谁都路由不到",
            )
        _validate_options(cls, options)
        _REGISTRY[kind] = SourceEntry(
            kind=kind,
            build=lambda opts=None: cls(**(opts or {})),
            markets=markets,
            options=options,
            note=note,
            module=cls.__module__.rsplit(".", 1)[-1],
            capabilities=provided_by(cls),
        )
        return cls

    return decorate


def _validate_options(cls: type, options: tuple[str, ...]) -> None:
    """声明的参数必须真存在——否则配置写错时拿到的是 TypeError，不是「这个参数不认识」。"""
    import inspect

    accepted = {
        p.name
        for p in inspect.signature(cls.__init__).parameters.values()
        if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    } - {"self"}
    unknown = [o for o in options if o not in accepted]
    if unknown:
        raise DatasourceError(
            "sources", f"{cls.__name__} 不接受参数 {unknown}，可传的是 {sorted(accepted)}"
        )


def known() -> dict[str, SourceEntry]:
    return dict(_REGISTRY)


def kinds() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def build(kind: str, options: Mapping[str, Any] | None = None) -> BarAdapter:
    """按实现名构造适配器。未知实现或非法参数都在这里当场拒绝。"""
    entry = _REGISTRY.get(kind)
    if entry is None:
        raise DatasourceError(
            "sources", f"数据源实现 {kind!r} 未注册；可用: {list(kinds())}"
        )
    given = dict(options or {})
    unexpected = [k for k in given if k not in entry.options]
    if unexpected:
        raise DatasourceError(
            "sources",
            f"数据源 {kind} 不接受参数 {sorted(unexpected)}；可配置的是 {list(entry.options)}",
        )
    return entry.build(given or None)


def discover() -> tuple[str, ...]:
    """导入本包里所有非基础设施模块，让它们的 `@source` 生效。

    写成「遍历包目录」而不是「在某个文件里列一份清单」：清单会漏，
    漏掉的源不是不存在，而是**看起来不存在**——排查时会一路去查网络。
    """
    package = __name__.rsplit(".", 1)[0]
    loaded = []
    # 本模块是包里的一个模块而不是包本身，`__path__` 在这里不存在
    for info in pkgutil.iter_modules([str(Path(__file__).parent)]):
        if info.name in _NOT_ADAPTERS or info.ispkg:
            continue
        importlib.import_module(f"{package}.{info.name}")
        loaded.append(info.name)
    return tuple(sorted(loaded))


def markets_of(kind: str) -> frozenset[str]:
    entry = _REGISTRY.get(kind)
    if entry is None:
        raise DatasourceError("sources", f"数据源实现 {kind!r} 未注册")
    return entry.markets


@dataclass(frozen=True)
class Catalog:
    """给管理后台/CLI 看的「可用实现」视图：名字、支持市场、可配参数、来自哪个文件。"""

    entries: tuple[SourceEntry, ...] = field(default_factory=tuple)

    def by_kind(self) -> dict[str, SourceEntry]:
        return {e.kind: e for e in self.entries}

    def supporting(self, market: str) -> tuple[str, ...]:
        return tuple(sorted(e.kind for e in self.entries if market in e.markets))


def catalog() -> Catalog:
    return Catalog(tuple(sorted(_REGISTRY.values(), key=lambda e: e.kind)))


discover()
