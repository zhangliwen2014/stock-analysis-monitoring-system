"""调度器的四种数据形状：job 吃到什么、吐出什么、怎么登记、靠什么活。

`JobOutcome.errors` 不是异常：一个 job 覆盖很多只票，其中三只取不到不该让
「另外七十只已经入库」这件事消失。所以失败与行数并存于同一个结果里——
读它的一侧（`task_runs` 的一行）要能同时看到「写了 70 行」和「3 只失败」。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from sams.core.clock import Clock
from sams.core.config import Settings
from sams.services.calendar import CalendarService
from sams.services.corp_actions import CorpActionSyncService
from sams.services.fundamentals import FundamentalSyncService
from sams.services.fx import FxSyncService
from sams.services.rules.engine import RuleEngine
from sams.services.rules.recheck import Rechecker
from sams.services.scheduler.due import DAILY, FX, MONTHLY, WEEKLY
from sams.services.scheduler.universe import Universe, UniverseBuilder
from sams.services.sync import SyncService
from sams.storage.repos.users import UserRepo

#: job 能声明的周期：值就是 `due.py` 算到期时认的那四个，这里只做一次转写，
#: 让登记 job 的 jobs.py 不必回头认识调度器的另一半
PERIODS = (DAILY, WEEKLY, MONTHLY, FX)


class RegistryView(Protocol):
    """调度器对数据源层的全部要求：问一句「这个市场这条能力链上有谁」。

    只声明这一个方法（与 `due.py` 的 `CalendarView` 同一做法），`AdapterRegistry` 恰好实现了它。
    不写死那个类，是为了让「装配一个 job 需要认识哪几层」这件事只有一条答案：
    取数一律走 services，调度器拿不到 `fetch_*`，也就顺手绕不过去。
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
    universe: UniverseBuilder
    sync: SyncService
    corp_actions: CorpActionSyncService
    fundamentals: FundamentalSyncService
    fx: FxSyncService
    calendar: CalendarService
    rules: RuleEngine
    recheck: Rechecker
    users: UserRepo
    clock: Clock
