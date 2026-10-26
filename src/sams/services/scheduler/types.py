"""调度器的四种数据形状：job 吃到什么、吐出什么、怎么登记、靠什么活。

`JobOutcome.errors` 不是异常：一个 job 覆盖很多只票，其中三只取不到不该让
「另外七十只已经入库」这件事消失。所以失败与行数并存于同一个结果里——
读它的一侧（`task_runs` 的一行）要能同时看到「写了 70 行」和「3 只失败」。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from sams.core.config import Settings
from sams.core.version import VersionGate
from sams.services.calendar import CalendarService
from sams.services.corp_actions import CorpActionSyncService
from sams.services.fundamentals import FundamentalSyncService
from sams.services.fx import FxSyncService
from sams.services.rules.engine import RuleEngine
from sams.services.rules.recheck import Rechecker
from sams.services.scheduler.universe import Universe
from sams.services.symbol_master import SymbolMasterSyncService
from sams.services.sync import SyncService
from sams.storage.repos.users import UserRepo


class RegistryView(Protocol):
    """调度器对数据源层只问一句话：「这个市场这条能力链上有谁」（`chain_for`）。

    与 `due.py` 的 `CalendarView` 同一做法——`AdapterRegistry` 恰好实现了这一个方法，
    这里不写死那个类。Protocol 真正买到的只有两件事：types/jobs 不 import 数据源层，
    以及它们向数据源层问的只有 `chain_for` 这一句。它不构成取数禁令——每个服务都把
    `registry` 存成了公开属性，`deps.sync.registry.fetch_*` 仍然够得着；拦在取数之外靠的是
    「job 一律走 services」这条约定，不是这个 Protocol。
    """

    def chain_for(self, market: str, method: str) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class JobContext:
    period_key: str
    as_of: date
    #: 市场 job 一定非空；全局 job（汇率）是 None
    market: str | None
    universe: Universe


@dataclass(frozen=True)
class JobOutcome:
    rows_affected: int = 0
    note: str = ""
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class JobSpec:
    name: str
    period: str
    market: str | None
    run: Callable[[JobContext], JobOutcome]

    @property
    def title(self) -> str:
        return f"{self.name}@{self.market or '全局'}"

    @property
    def component(self) -> str:
        """系统告警里的组件名：去重按它（D102），所以它必须跨进程稳定。"""
        return f"job.{self.name}.{self.market or 'global'}"


@dataclass(frozen=True)
class JobDeps:
    settings: Settings
    registry: RegistryView
    sync: SyncService
    corp_actions: CorpActionSyncService
    fundamentals: FundamentalSyncService
    fx: FxSyncService
    calendar: CalendarService
    rules: RuleEngine
    recheck: Rechecker
    users: UserRepo
    #: 盘中缓存的失效闸：日线/公司行为写入后 bump，缓存整表清（§5.4）。
    #: 默认自建一份，job 那一头永远敲得到；装配根（Task 15）会把盘中线程用的那一份
    #: 传进来——两边各拿一份计数的话，盘后的 bump 清不掉盘中那份缓存
    versions: VersionGate = field(default_factory=VersionGate)
    #: 代码表周同步（D141）：带默认值，所以没装这一层的部署照旧能构造 `JobDeps`——
    #: `build_jobs` 见到 None 就不登记那个 job，而不是起不来
    symbol_master: SymbolMasterSyncService | None = None
