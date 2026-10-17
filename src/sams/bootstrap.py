"""组装根：全项目唯一同时认识所有下层实现的地方。

分层规则是 core ← {storage, datasource} ← services ← cli，模块之间不横向引用；
只有这里可以 import 具体实现。测试要替换任何一个部件，都从 build_container
的关键参数下手，不需要 monkeypatch。
"""

from __future__ import annotations

from dataclasses import dataclass

from sams.core.calendar import CalendarProvider
from sams.core.config import Settings
from sams.core.events import EventBus
from sams.datasource.base import BarAdapter
from sams.datasource.calendar_providers import default_providers
from sams.datasource.registry import AdapterRegistry
from sams.services.accounts import AccountService
from sams.services.calendar import CalendarService
from sams.services.health import HealthService
from sams.services.sync import SyncService
from sams.storage.db import Database
from sams.storage.migrate import applied, apply_migrations, pending
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.calendar import CalendarRepo
from sams.storage.repos.sessions import SessionRepo
from sams.storage.repos.source_health import SourceHealthRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.users import UserRepo
from sams.storage.repos.watchlists import WatchlistRepo


@dataclass
class Container:
    settings: Settings
    db: Database
    bus: EventBus
    users: UserRepo
    sessions: SessionRepo
    symbols: SymbolRepo
    bars: BarRepo
    watchlists: WatchlistRepo
    calendar_store: CalendarRepo
    source_health: SourceHealthRepo
    registry: AdapterRegistry
    accounts: AccountService
    calendar: CalendarService
    sync: SyncService
    health: HealthService

    def close(self) -> None:
        self.db.close()

    # 迁移是显式运维动作，CLI 经由容器触发，不直接 import storage
    def migrate(self) -> list[str]:
        return apply_migrations(self.db)

    def pending_migrations(self) -> list[str]:
        return pending(self.db)

    def applied_migrations(self) -> set[str]:
        return applied(self.db)


def build_container(
    settings: Settings | None = None,
    *,
    db: Database | None = None,
    bus: EventBus | None = None,
    adapters: dict[str, BarAdapter] | None = None,
    providers: dict[str, CalendarProvider] | None = None,
) -> Container:
    cfg = settings or Settings.load()
    database = db or Database.open(cfg.db_path)
    event_bus = bus or EventBus()
    bars = BarRepo(database)
    symbols = SymbolRepo(database)
    calendar_store = CalendarRepo(database)
    users, sessions = UserRepo(database), SessionRepo(database)
    source_health = SourceHealthRepo(database)

    registry = AdapterRegistry.from_settings(cfg, adapters=adapters, store=source_health)
    providers = providers if providers is not None else default_providers()
    calendar = CalendarService(calendar_store, providers, bus=event_bus)

    return Container(
        settings=cfg,
        db=database,
        bus=event_bus,
        users=users,
        sessions=sessions,
        symbols=symbols,
        bars=bars,
        watchlists=WatchlistRepo(database),
        calendar_store=calendar_store,
        source_health=source_health,
        registry=registry,
        accounts=AccountService(users, sessions),
        calendar=calendar,
        sync=SyncService(registry, bars, symbols, cfg.markets),
        health=HealthService(database, registry, calendar_store, cfg),
    )
