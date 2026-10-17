"""组装根：全项目唯一同时认识所有下层实现的地方。

分层规则是 core ← {storage, datasource} ← services ← cli，模块之间不横向引用；
只有这里可以 import 具体实现。测试要替换任何一个部件，都从 build_container
的关键参数下手，不需要 monkeypatch。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sams.core.calendar import CalendarProvider
from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.config import Settings
from sams.core.events import EventBus, SystemAlarm
from sams.datasource.base import BarAdapter
from sams.datasource.calendar_providers import default_providers
from sams.datasource.registry import AdapterRegistry
from sams.datasource.sources import build as build_source
from sams.services.accounts import AccountService
from sams.services.analysis import AnalysisService
from sams.services.calendar import CalendarService
from sams.services.corp_actions import CorpActionSyncService
from sams.services.health import HealthService
from sams.services.sources import SourceAdminService
from sams.services.sync import SyncService
from sams.storage.db import Database
from sams.storage.migrate import applied, apply_migrations, pending
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.calendar import CalendarRepo
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.corp_actions_coverage import CorpActionsCoverageRepo
from sams.storage.repos.data_sources import DataSourceRepo
from sams.storage.repos.sessions import SessionRepo
from sams.storage.repos.source_health import SourceHealthRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.users import UserRepo
from sams.storage.repos.watchlists import WatchlistRepo

logger = logging.getLogger("sams.bootstrap")


def attach_log_sink(bus: EventBus) -> None:
    """让 SystemAlarm 至少有一条落地路径。

    总线没有订阅者时 publish 是静默 no-op：spec §12 的「采集连续失败降系统告警」
    会在没人察觉的情况下不成立。日志是那条最小路径，
    M3 的 admin 通知渠道订阅同一事件即可，不必改这里。
    """

    def log_alarm(event: SystemAlarm) -> None:
        logger.error("[alarm] %s: %s", event.component, event.message)

    bus.subscribe(SystemAlarm, log_alarm)


@dataclass
class Container:
    settings: Settings
    db: Database
    bus: EventBus
    clock: Clock
    users: UserRepo
    sessions: SessionRepo
    symbols: SymbolRepo
    bars: BarRepo
    corp_actions: CorpActionRepo
    corp_actions_coverage: CorpActionsCoverageRepo
    watchlists: WatchlistRepo
    calendar_store: CalendarRepo
    source_health: SourceHealthRepo
    data_sources: DataSourceRepo
    registry: AdapterRegistry
    accounts: AccountService
    calendar: CalendarService
    sync: SyncService
    corp_action_sync: CorpActionSyncService
    analysis: AnalysisService
    health: HealthService
    source_admin: SourceAdminService

    def close(self) -> None:
        self.db.close()

    # 迁移是显式运维动作，CLI 经由容器触发，不直接 import storage
    def migrate(self) -> list[str]:
        return apply_migrations(self.db)

    def pending_migrations(self) -> list[str]:
        return pending(self.db)

    def applied_migrations(self) -> set[str]:
        return applied(self.db)


def _registry(
    cfg: Settings,
    data_sources: DataSourceRepo,
    source_health: SourceHealthRepo,
    event_bus: EventBus,
    clock: Clock,
    adapters: dict[str, BarAdapter] | None,
) -> AdapterRegistry:
    """数据源链的来源：库里有记录就按库里的，没有才退回配置文件。

    显式传 `adapters` 是测试与特殊场景的后门，传了就整个绕过库——
    否则单测要在 DuckDB 里播种子数据才能构造容器，而「装配」这件事本身就该可替换。
    """
    if adapters is not None:
        return AdapterRegistry.from_settings(
            cfg, adapters=adapters, store=source_health, bus=event_bus, clock=clock
        )
    rows = data_sources.list()
    if not rows:
        return AdapterRegistry.from_settings(
            cfg, store=source_health, bus=event_bus, clock=clock
        )
    built = {row.name: build_source(row.kind, row.options) for row in rows if row.enabled}
    chains = data_sources.chains()
    dark = sorted(set(cfg.markets) - set(chains))
    if dark:
        # 不抛错：健康检查与账号管理不该因为「某个市场暂时没源」就整个跑不起来。
        # 但必须说出来——配置被删空的正常症状是「下次采集才知道」
        logger.warning("市场 %s 在库里没有任何启用的数据源，采集会直接失败", dark)
    return AdapterRegistry.from_settings(
        cfg,
        adapters=built,
        market_sources=chains,
        store=source_health,
        bus=event_bus,
        clock=clock,
    )


def build_container(
    settings: Settings | None = None,
    *,
    db: Database | None = None,
    bus: EventBus | None = None,
    clock: Clock | None = None,
    adapters: dict[str, BarAdapter] | None = None,
    providers: dict[str, CalendarProvider] | None = None,
) -> Container:
    cfg = settings or Settings.load()
    database = db or Database.open(cfg.db_path)
    event_bus = bus or EventBus()
    attach_log_sink(event_bus)
    clock = clock or SYSTEM_CLOCK
    bars = BarRepo(database)
    symbols = SymbolRepo(database)
    calendar_store = CalendarRepo(database)
    users = UserRepo(database)
    sessions = SessionRepo(database, clock)
    corp_actions = CorpActionRepo(database)
    corp_actions_coverage = CorpActionsCoverageRepo(database)
    source_health = SourceHealthRepo(database)
    data_sources = DataSourceRepo(database)

    registry = _registry(cfg, data_sources, source_health, event_bus, clock, adapters)
    providers = providers if providers is not None else default_providers()
    calendar = CalendarService(calendar_store, providers, bus=event_bus, clock=clock)

    return Container(
        settings=cfg,
        db=database,
        bus=event_bus,
        clock=clock,
        users=users,
        sessions=sessions,
        symbols=symbols,
        bars=bars,
        corp_actions=corp_actions,
        corp_actions_coverage=corp_actions_coverage,
        watchlists=WatchlistRepo(database),
        calendar_store=calendar_store,
        source_health=source_health,
        data_sources=data_sources,
        source_admin=SourceAdminService(data_sources, cfg),
        registry=registry,
        accounts=AccountService(users, sessions),
        calendar=calendar,
        sync=SyncService(registry, bars, symbols, cfg.markets),
        corp_action_sync=CorpActionSyncService(
            registry, corp_actions, corp_actions_coverage, clock=clock
        ),
        analysis=AnalysisService(bars, corp_actions, corp_actions_coverage, clock=clock),
        health=HealthService(database, registry, calendar_store, cfg, clock=clock),
    )
