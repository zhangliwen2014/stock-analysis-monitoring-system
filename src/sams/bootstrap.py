"""组装根：全项目唯一同时认识所有下层实现的地方。

分层规则是 core ← {storage, datasource} ← services ← cli，模块之间不横向引用；
只有这里可以 import 具体实现。测试要替换任何一个部件，都从 build_container
的关键参数下手，不需要 monkeypatch。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from sams.core.calendar import CalendarProvider
from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.config import Settings
from sams.core.events import EventBus, SystemAlarm
from sams.core.notify import AlertMessage
from sams.datasource.base import BarAdapter
from sams.datasource.calendar_providers import default_providers
from sams.datasource.registry import AdapterRegistry
from sams.datasource.sources import build as build_source
from sams.monitor.notifiers import Notifier, build_notifier_factory
from sams.monitor.router import NotifyService
from sams.services.accounts import AccountService
from sams.services.analysis import AnalysisService
from sams.services.calendar import CalendarService
from sams.services.corp_actions import CorpActionSyncService
from sams.services.fundamentals import FundamentalService, FundamentalSyncService
from sams.services.fx import FxSyncService
from sams.services.health import HealthService
from sams.services.ledger import LedgerService
from sams.services.portfolio import PortfolioService
from sams.services.rules.derive import RuleDeriver
from sams.services.rules.engine import RuleEngine
from sams.services.rules.maintenance import RuleService
from sams.services.rules.recheck import Rechecker
from sams.services.rules.snapshot import RuleData
from sams.services.scheduler.alarms import SystemAlerter
from sams.services.scheduler.jobs import build_jobs
from sams.services.scheduler.runner import JobRunner
from sams.services.scheduler.service import Scheduler
from sams.services.scheduler.types import JobDeps
from sams.services.scheduler.universe import UniverseBuilder
from sams.services.screen import ScreenService
from sams.services.sources import SourceAdminService
from sams.services.sync import SyncService
from sams.services.theses import ThesisService
from sams.services.valuation import ValuationService
from sams.storage.crypto import Cipher, cipher_for
from sams.storage.db import Database
from sams.storage.migrate import applied, apply_migrations, pending
from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.calendar import CalendarRepo
from sams.storage.repos.channels import ChannelRepo
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.corp_actions_coverage import CorpActionsCoverageRepo
from sams.storage.repos.data_sources import DataSourceRepo
from sams.storage.repos.financials import FinancialRepo, FundingFlowRepo, ValuationRepo
from sams.storage.repos.fx import FxRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.rules import RuleRepo
from sams.storage.repos.scan_results import ScanResultRepo
from sams.storage.repos.sessions import SessionRepo
from sams.storage.repos.source_health import SourceHealthRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.task_runs import HeartbeatRepo, TaskRunRepo
from sams.storage.repos.theses import ThesisRepo
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
    # 三个基本面仓库带 `_store`：`container.financials` 与 `container.fundamentals`
    # 只差一个后缀，而一个是仓库、一个是读取用例，混用的代价是读代码的人猜
    financials_store: FinancialRepo
    valuation_store: ValuationRepo
    flow_store: FundingFlowRepo
    scan_store: ScanResultRepo
    watchlists: WatchlistRepo
    #: 汇率仓库：`fx` 这个名字留给服务（M3-A 的 `sams sync fx`），带 `_store` 是为了不误用
    fx_store: FxRepo
    #: thesis 的仓库带 `_store`：`container.theses` 是那个读取用例（写入口只有一个）
    theses_store: ThesisRepo
    calendar_store: CalendarRepo
    source_health: SourceHealthRepo
    data_sources: DataSourceRepo
    registry: AdapterRegistry
    accounts: AccountService
    calendar: CalendarService
    sync: SyncService
    corp_action_sync: CorpActionSyncService
    fundamental_sync: FundamentalSyncService
    #: 汇率那条链：没有市场维度，所以它不跟着 `sync`/`fundamental_sync` 走市场路由
    fx_sync: FxSyncService
    fundamentals: FundamentalService
    screen: ScreenService
    analysis: AnalysisService
    #: 估值合成（spec §8.3）：它吃上面那两个读数用例，不是并列的第三路取数
    valuation: ValuationService
    #: 观点的生命周期：它吃上面那个仓库与那份估值读数（spec §9.1）
    theses: ThesisService
    #: 账本的写用例：一次成交跨订单/持仓/现金/流水四张表，入口只有这一个（§5.4）
    ledger: LedgerService
    #: 持仓与汇总的读用例：它只读账本，不改账本
    portfolio: PortfolioService
    #: SIM 的账户仓库：`accounts` 这个名字属于用户账号服务（`AccountService`），
    #: 两个都对得上名字的东西放在一起一定会被误用，所以这里带 `sim_` 前缀
    sim_accounts: SimAccountRepo
    #: 成交/持仓/现金的那个仓库（订单历史 + 状态）
    sim_ledger: SimOrderRepo
    channels_store: ChannelRepo
    alerts_store: AlertRepo
    #: 通道凭据的加密器：CLI 要能在不碰 `storage.services.*` 的情况下掩码
    cipher: Cipher
    #: 发送路由（spec §11）
    notify: NotifyService
    health: HealthService
    source_admin: SourceAdminService
    #: 规则仓库：`rules` 那个名字留给 T-17 的服务，仓库带 `_store`（与 theses/fx 同一命名法）
    rules_store: RuleRepo
    #: 规则引擎的取数装配（spec §10）
    rule_data: RuleData
    #: 一趟求值（`sams rule check` 的出口，T-18 的命令行按它接线）
    rule_engine: RuleEngine
    #: 规则的维护面（T-17）：四样仓库层看不见的检查 + 改参数时回写论上那一列
    rule_service: RuleService
    #: §9.2 建仓派生（`sams rule derive`）
    rule_deriver: RuleDeriver
    #: 周复检（§9.3，`sams rule recheck`）
    rule_rechecker: Rechecker
    #: 常驻调度器（spec §12）：`sams run start|once|jobs` 与健康页的 `[调度]` 读的是同一台
    scheduler: Scheduler

    def close(self) -> None:
        self.db.close()

    # 迁移是显式运维动作，CLI 经由容器触发，不直接 import storage
    def migrate(self) -> list[str]:
        return apply_migrations(self.db)

    def pending_migrations(self) -> list[str]:
        return pending(self.db)

    def applied_migrations(self) -> set[str]:
        return applied(self.db)


class _NotifyAlertSender:
    """把 `NotifyService.dispatch` 的 `DispatchResult` 折成 id 串（D95）。

    只有四行的适配器不是多余的一层：`services` 不许 import `monitor`，而
    `DispatchResult` 住在那里。引擎要的只有 `alert_id`，别的那些（每条通道的
    `DeliveryReport`）留在 `monitor` 那一头，由 `sams alert show` 去查。
    """

    def __init__(self, notify: NotifyService) -> None:
        self._notify = notify

    def dispatch(
        self,
        message: AlertMessage,
        *,
        user_id: str,
        channel_ids: Sequence[str] = (),
        rule_id: str | None = None,
        account_id: str | None = None,
    ) -> str:
        return self._notify.dispatch(
            message,
            user_id=user_id,
            channel_ids=channel_ids,
            rule_id=rule_id,
            account_id=account_id,
        ).alert_id


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
    notifier_factory: Callable[[str, Mapping[str, object]], Notifier] | None = None,
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
    financials_store = FinancialRepo(database)
    valuation_store = ValuationRepo(database)
    flow_store = FundingFlowRepo(database)
    scan_store = ScanResultRepo(database)
    theses_store = ThesisRepo(database)
    source_health = SourceHealthRepo(database)
    data_sources = DataSourceRepo(database)

    registry = _registry(cfg, data_sources, source_health, event_bus, clock, adapters)
    providers = providers if providers is not None else default_providers()
    calendar = CalendarService(calendar_store, providers, bus=event_bus, clock=clock)
    fundamentals = FundamentalService(financials_store, valuation_store, clock=clock)
    analysis = AnalysisService(bars, corp_actions, corp_actions_coverage, symbols, clock=clock)
    valuation = ValuationService(
        fundamentals, analysis, financials_store, valuation_store, flow_store, clock=clock
    )
    sim_accounts = SimAccountRepo(database)
    sim_orders = SimOrderRepo(database, sim_accounts, markets=cfg.markets, symbols=symbols)
    ledger = LedgerService(sim_accounts, sim_orders, theses_store, users, cfg)
    fx_store = FxRepo(database)
    portfolio = PortfolioService(sim_accounts, sim_orders, bars, fx_store, users, clock=clock)
    cipher = cipher_for(cfg)
    channels_store = ChannelRepo(database, cipher=cipher, clock=clock)
    alerts_store = AlertRepo(database, clock=clock)
    notify = NotifyService(
        channels_store,
        alerts_store,
        users,
        bus=event_bus,
        factory=notifier_factory if notifier_factory is not None else build_notifier_factory(),
    )

    watchlists = WatchlistRepo(database)
    rules_store = RuleRepo(database, clock=clock)
    rule_data = RuleData(
        valuation=valuation,
        analysis=analysis,
        bars=bars,
        symbols=symbols,
        accounts=sim_accounts,
        ledger=sim_orders,
        theses=theses_store,
        watchlists=watchlists,
        settings=cfg,
    )
    # 规则告警与系统告警共用这一个 sender：DND、通道选择、`notify_log` 那一行都住在
    # `NotifyService` 那一头，一个组装根里留两条发送路径，迟早一条带上这些而另一条不带
    sender = _NotifyAlertSender(notify)
    rule_engine = RuleEngine(
        rules=rules_store,
        alerts=alerts_store,
        data=rule_data,
        calendar=calendar,
        fx=fx_store,
        bus=event_bus,
        sender=sender,
        settings=cfg,
        clock=clock,
    )
    # 三个规则服务排在最后：它们吃的就是上面那些已经装好的仓库与取数处，
    # 而 `RuleService` 要的是 `rule_data`（notes 里那句「展开成零个对象」由它算）
    rule_service = RuleService(
        rules=rules_store,
        theses=theses_store,
        accounts=sim_accounts,
        channels=channels_store,
        users=users,
        data=rule_data,
        db=database,
        settings=cfg,
        clock=clock,
    )
    rule_deriver = RuleDeriver(rules_store, theses_store)
    rule_rechecker = Rechecker(
        theses=theses_store, data=rule_data, bus=event_bus, clock=clock
    )

    # D102：系统告警与规则告警共用上面那个 sender，订阅的是同一条总线
    SystemAlerter(
        alerts=alerts_store, users=users, sender=sender, settings=cfg, clock=clock
    ).attach(event_bus)

    # 下面四个同步服务与 `UniverseBuilder` 原本就地造在 `return Container(...)` 的参数里。
    # 调度器也要吃它们，而同一个服务被造两份时，容器里那个与 job 里那个不是同一个对象：
    # 测试替换或事后调一处参数，另一处照旧跑——症状不是报错，是「改了没生效」
    sync = SyncService(registry, bars, symbols, cfg.markets)
    corp_action_sync = CorpActionSyncService(
        registry, corp_actions, corp_actions_coverage, clock=clock
    )
    fundamental_sync = FundamentalSyncService(
        registry, financials_store, valuation_store, flow_store, symbols
    )
    fx_sync = FxSyncService(registry, fx_store)
    universe_builder = UniverseBuilder(
        users=users,
        watchlists=watchlists,
        accounts=sim_accounts,
        orders=sim_orders,
        rules=rules_store,
        theses=theses_store,
        data=rule_data,
        settings=cfg,
    )
    task_runs = TaskRunRepo(database, clock=clock)
    heartbeats = HeartbeatRepo(database, clock=clock)
    deps = JobDeps(
        settings=cfg,
        registry=registry,
        sync=sync,
        corp_actions=corp_action_sync,
        fundamentals=fundamental_sync,
        fx=fx_sync,
        calendar=calendar,
        rules=rule_engine,
        recheck=rule_rechecker,
        users=users,
    )
    scheduler = Scheduler(
        cfg,
        build_jobs(deps),
        runner=JobRunner(runs=task_runs, bus=event_bus, clock=clock),
        runs=task_runs,
        heartbeats=heartbeats,
        universe=universe_builder,
        users=users,
        calendar=calendar,
        clock=clock,
    )

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
        financials_store=financials_store,
        valuation_store=valuation_store,
        flow_store=flow_store,
        scan_store=scan_store,
        theses_store=theses_store,
        watchlists=watchlists,
        fx_store=fx_store,
        calendar_store=calendar_store,
        source_health=source_health,
        data_sources=data_sources,
        source_admin=SourceAdminService(data_sources, cfg),
        registry=registry,
        accounts=AccountService(users, sessions),
        calendar=calendar,
        sync=sync,
        corp_action_sync=corp_action_sync,
        fundamental_sync=fundamental_sync,
        fx_sync=fx_sync,
        fundamentals=fundamentals,
        screen=ScreenService(
            financials_store, valuation_store, symbols, scan_store, clock=clock
        ),
        analysis=analysis,
        valuation=valuation,
        # `settings` 是那个「已知市场」的出处：一个没配的市场立不出 thesis，
        # 因为那条链上每一栏都读不到数（D84）
        theses=ThesisService(theses_store, users, valuation, cfg, clock=clock, orders=sim_orders),
        ledger=ledger,
        portfolio=portfolio,
        sim_accounts=sim_accounts,
        sim_ledger=sim_orders,
        channels_store=channels_store,
        alerts_store=alerts_store,
        rules_store=rules_store,
        rule_data=rule_data,
        rule_engine=rule_engine,
        rule_service=rule_service,
        rule_deriver=rule_deriver,
        rule_rechecker=rule_rechecker,
        scheduler=scheduler,
        cipher=cipher,
        notify=notify,
        health=HealthService(
            database,
            registry,
            calendar_store,
            cfg,
            clock=clock,
            sim_accounts=sim_accounts,
            sim_ledger=sim_orders,
            fx=fx_store,
            # 本机通道的可用性由 monitor 探测，health 只接这一个函数：多一条 import 就红了
            local_status=notify.local_status,
            # 健康页读的就是上面这台调度器留下的那两张台账：不是「它此刻活着吗」——
            # 调度器持有库文件时，第二个进程连不上（DuckDB 的独占锁），这一屏说的是
            # 「它上一次写下结论是什么时候」
            schedule=scheduler.schedule_data,
        ),
    )
