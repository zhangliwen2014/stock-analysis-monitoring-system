"""组装根：全项目唯一同时认识所有下层实现的地方。

分层规则是 core ← {storage, datasource} ← services ← cli，模块之间不横向引用；
只有这里可以 import 具体实现。测试要替换任何一个部件，都从 build_container
的关键参数下手，不需要 monkeypatch。
"""

from __future__ import annotations

import logging
import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields

from sams.core.calendar import CalendarProvider
from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.config import Settings
from sams.core.events import EventBus, SystemAlarm
from sams.core.notify import AlertMessage
from sams.core.version import VersionGate
from sams.datasource.base import BarAdapter
from sams.datasource.calendar_providers import default_providers
from sams.datasource.registry import AdapterRegistry
from sams.datasource.sources import build as build_source
from sams.monitor.notifiers import Notifier, build_notifier_factory
from sams.monitor.queue import (
    AlertQueue,
    NotifyWorker,
    QueuedAlertSender,
    SystemAlarmRelay,
)
from sams.monitor.router import NotifyService
from sams.services.accounts import AccountService
from sams.services.analysis import AnalysisService
from sams.services.calendar import CalendarService
from sams.services.corp_actions import CorpActionSyncService
from sams.services.fundamentals import FundamentalService, FundamentalSyncService
from sams.services.fx import FxSyncService
from sams.services.health import HealthService
from sams.services.intraday import IntradayMonitor
from sams.services.ledger import LedgerService
from sams.services.portfolio import PortfolioService
from sams.services.prune import PruneService
from sams.services.quotes import QuoteBoard, QuoteService
from sams.services.rules.cache import SnapshotCache
from sams.services.rules.derive import RuleDeriver
from sams.services.rules.engine import RuleEngine
from sams.services.rules.intraday import IntradayEvaluator
from sams.services.rules.maintenance import RuleService
from sams.services.rules.recheck import Rechecker
from sams.services.rules.snapshot import RuleData
from sams.services.runtime import Runtime
from sams.services.scheduler.alarms import SystemAlerter
from sams.services.scheduler.beatfile import beat_path
from sams.services.scheduler.jobs import build_jobs
from sams.services.scheduler.report import ScheduleData, collect
from sams.services.scheduler.runner import JobRunner
from sams.services.scheduler.service import RunRequestQueue, Scheduler
from sams.services.scheduler.types import JobDeps, JobSpec
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
from sams.storage.repos.quotes import QuoteHealthRepo, QuoteRepo
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


#: 已经挂过 sink 的总线。`serve` 里 core 只造一次，但 CLI 的测试与「重建装配」
#: 会反复经过这里：同一个 bus 上挂两个 sink，一条系统告警会在日志里出现两遍。
_SINKED: weakref.WeakSet[EventBus] = weakref.WeakSet()


def attach_log_sink(bus: EventBus) -> None:
    """让 SystemAlarm 至少有一条落地路径（同一个 bus 只挂一次）。

    总线没有订阅者时 publish 是静默 no-op：spec §12 的「采集连续失败降系统告警」
    会在没人察觉的情况下不成立。日志是那条最小路径，
    M3 的 admin 通知渠道订阅同一事件即可，不必改这里。
    """
    if bus in _SINKED:
        return

    def log_alarm(event: SystemAlarm) -> None:
        logger.error("[alarm] %s: %s", event.component, event.message)

    bus.subscribe(SystemAlarm, log_alarm)
    _SINKED.add(bus)


@dataclass(frozen=True)
class AppCore:
    """进程级共享件：后台线程与 HTTP 线程都拿这一份（D113）。

    `registry_db` 只服务 registry 的源健康落库；在 `build_serve` 里它不是任何
    线程上下文的那条连接，而 `build_container` 让两者共用同一条连接，
    以保持 CLI「一个进程一条连接」的形状。
    """

    settings: Settings
    clock: Clock
    bus: EventBus
    cipher: Cipher
    registry: AdapterRegistry
    #: 手动请求队列：调度线程与 Web 共用，Web 的「立即同步」才落得到同一台调度器
    requests: RunRequestQueue
    #: 行情板是进程级的一份：盘中线程写、`/healthz` 读（Web 页面在计划 3 接入后也读）。
    #: 每个上下文各造一块板时，页面看到的永远是自己那块空板
    board: QuoteBoard
    #: 缓存失效的闸：四条线程的写面与盘中那份缓存读的是同一份，各造一份时
    #: bump 响在自己那份上，缓存那头永远不动（§5.4）
    versions: VersionGate
    #: 告警队列：盘中线程 offer、通知线程取——四条线程里只有它横跨三条
    alerts_queue: AlertQueue
    registry_db: Database

    def close(self) -> None:
        # build_container 里 registry_db 与上下文共用同一条连接：两边各有一个 close，
        # 重复 close 无害，关一边就够
        self.registry_db.close()


@dataclass
class ServiceContext:
    """一条线程的一整套仓库与服务：一条 DuckDB 连接，不跨线程（D113）。

    每条上下文各造一台调度器，在 `build_serve` 里只有调度线程会 `run_forever`；
    请求队列来自 `AppCore.requests`，所以别的线程 `request_run` 入队的是同一批请求。
    """

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
    #: 报价快照与报价源健康：盘中线程写、Web 读，各走自己的连接
    quotes_store: QuoteRepo
    quote_health: QuoteHealthRepo
    #: 取价服务：盘中线程那一份会真去问源；其它上下文也有，但没人调用 `poll`
    quotes: QuoteService
    #: 盘中节拍器：同样只有盘中线程那一份被 `run_forever`
    intraday: IntradayMonitor
    task_runs: TaskRunRepo
    heartbeats: HeartbeatRepo
    #: 保留策略（D116）：CLI 与 serve 的常驻维护（以及计划 3 的 admin 健康页）共用这一份
    prune: PruneService
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


@dataclass
class Container(ServiceContext):
    """CLI 的容器：与线程级上下文同一形状，名字留给 CLI 的签名。"""


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


def build_core(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    bus: EventBus | None = None,
    database: Database | None = None,
    adapters: dict[str, BarAdapter] | None = None,
) -> AppCore:
    """进程级共享件，一次进程只造一份。

    `database` 给进来时（`build_container` 就这么做）registry 的源健康与 CLI 的其余
    仓库共用一条连接；不给就自己开一条，只给 registry 用。在 `build_serve` 里调度、
    盘中、维护三条上下文各开各的连接，不碰这一条。自己开的那条在装配失败时自己关；
    传进来的不碰。
    """
    cfg = settings or Settings.load()
    event_bus = bus or EventBus()
    attach_log_sink(event_bus)
    clock = clock or SYSTEM_CLOCK
    core_db = database or Database.open(cfg.db_path)
    try:
        source_health = SourceHealthRepo(core_db)
        data_sources = DataSourceRepo(core_db)
        registry = _registry(cfg, data_sources, source_health, event_bus, clock, adapters)
        # 密钥坏了也是装配失败：在 return 里求值时它会绕过下面那只 except，连接就漏了
        cipher = cipher_for(cfg)
    except BaseException:
        # 自己开的那条在装配失败时由自己关上：调用方拿不到引用，半成品的句柄会一直握着文件
        if database is None:
            core_db.close()
        raise
    return AppCore(
        settings=cfg,
        clock=clock,
        bus=event_bus,
        cipher=cipher,
        registry=registry,
        requests=RunRequestQueue(),
        board=QuoteBoard(),
        versions=VersionGate(),
        alerts_queue=AlertQueue(),
        registry_db=core_db,
    )


def _context_on(
    core: AppCore,
    *,
    database: Database,
    providers: dict[str, CalendarProvider] | None,
    notifier_factory: Callable[[str, Mapping[str, object]], Notifier] | None,
    queue: AlertQueue | None = None,
    cache: SnapshotCache | None = None,
) -> ServiceContext:
    """在给定连接上装一整套仓库与服务。连接的生命周期归调用方。

    `queue` 与 `cache` 只有盘中那一份上下文传：传了 queue 就改走「落库 + 入队」，
    投递交给通知线程；传了 cache 就让引擎每轮只读一次日线（§5.4）。
    """
    cfg = core.settings
    clock = core.clock
    event_bus = core.bus
    registry = core.registry
    cipher = core.cipher
    versions = core.versions
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
    quotes_store = QuoteRepo(database)
    quote_health = QuoteHealthRepo(database)
    quotes = QuoteService(
        registry,
        core.board,
        quotes_store,
        quote_health,
        clock=clock,
        # 与盘后同步共用同一个桶：两条线程各自限速等于把 rate_limit_per_s 放大一倍
        bucket=registry.bucket,
        bus=event_bus,
    )

    providers = providers if providers is not None else default_providers(cfg)
    calendar = CalendarService(calendar_store, providers, bus=event_bus, clock=clock)
    fundamentals = FundamentalService(financials_store, valuation_store, clock=clock)
    analysis = AnalysisService(bars, corp_actions, corp_actions_coverage, symbols, clock=clock)
    valuation = ValuationService(
        fundamentals, analysis, financials_store, valuation_store, flow_store, clock=clock
    )
    sim_accounts = SimAccountRepo(database)
    sim_orders = SimOrderRepo(database, sim_accounts, markets=cfg.markets, symbols=symbols)
    ledger = LedgerService(
        sim_accounts, sim_orders, theses_store, users, cfg, versions=versions
    )
    fx_store = FxRepo(database)
    portfolio = PortfolioService(sim_accounts, sim_orders, bars, fx_store, users, clock=clock)
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
    sender = (
        QueuedAlertSender(notify, queue) if queue is not None else _NotifyAlertSender(notify)
    )
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
        cache=cache,
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
        versions=versions,
    )
    rule_deriver = RuleDeriver(rules_store, theses_store, versions=versions)
    rule_rechecker = Rechecker(
        theses=theses_store, data=rule_data, bus=event_bus, clock=clock
    )

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
    on_poll = (
        None
        if queue is None
        else IntradayEvaluator(rules_store, rule_engine, core.board).after_poll
    )
    intraday = IntradayMonitor(
        cfg, quotes, calendar, universe_builder, clock=clock, on_poll=on_poll
    )
    task_runs = TaskRunRepo(database, clock=clock)
    heartbeats = HeartbeatRepo(database, clock=clock)
    prune = PruneService(
        database,
        quotes=quotes_store,
        task_runs=task_runs,
        heartbeats=heartbeats,
        clock=clock,
    )
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
        versions=versions,
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
        # 库被这个循环握着时，外部读者只能读库旁那一拍（D111）。
        # 内存库里 `beat_path` 是 `None`，什么也不写
        beat_path=beat_path(database.path),
        # 所有上下文共用同一只队列：只有调度线程跑循环，但谁都能往里放请求
        requests=core.requests,
    )

    return ServiceContext(
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
        theses=ThesisService(
            theses_store,
            users,
            valuation,
            cfg,
            clock=clock,
            orders=sim_orders,
            versions=versions,
        ),
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
        quotes_store=quotes_store,
        quote_health=quote_health,
        quotes=quotes,
        intraday=intraday,
        task_runs=task_runs,
        heartbeats=heartbeats,
        prune=prune,
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
            quote_health=quote_health,
            # 本机通道的可用性由 monitor 探测，health 只接这一个函数：多一条 import 就红了
            local_status=notify.local_status,
            # 健康页读的就是上面这台调度器留下的那两张台账：不是「它此刻活着吗」——
            # 调度器持有库文件时，第二个进程连不上（DuckDB 的独占锁），这一屏说的是
            # 「它上一次写下结论是什么时候」
            schedule=scheduler.schedule_data,
        ),
    )


def build_context(
    core: AppCore,
    *,
    database: Database | None = None,
    providers: dict[str, CalendarProvider] | None = None,
    notifier_factory: Callable[[str, Mapping[str, object]], Notifier] | None = None,
    queue: AlertQueue | None = None,
    cache: SnapshotCache | None = None,
) -> ServiceContext:
    """一条线程的一整套仓库与服务。不传 `database` 时自己开一条（D113）。

    自己开的那一条在装配失败时由自己关上：调用方手里没有它的引用，
    而半成品的句柄会一直握着库文件（Windows 上还会挡住删除）。
    传进来的连接归调用方，失败时也不许关。

    `queue` 与 `cache` 透传给 `_context_on`：只有盘中那一份上下文传。
    """
    owned = database is None
    database = database or Database.open(core.settings.db_path)
    try:
        return _context_on(
            core,
            database=database,
            providers=providers,
            notifier_factory=notifier_factory,
            queue=queue,
            cache=cache,
        )
    except BaseException:
        if owned:
            database.close()
        raise


def attach_system_alerter(context: ServiceContext) -> SystemAlerter:
    """把系统告警写进管理员收件箱；一个进程只挂一次。

    `build_context` 不挂：`serve` 里每条线程一个上下文，各挂一次会让同一条系统告警
    变成 N 条管理员告警。CLI 在 `build_container` 末尾挂；`serve` 那条挂在 `build_serve`：
    经 `SystemAlarmRelay` 只入队，处理在通知线程（计划 2）。
    """
    alerter = SystemAlerter(
        alerts=context.alerts_store,
        users=context.users,
        sender=_NotifyAlertSender(context.notify),
        settings=context.settings,
        clock=context.clock,
    )
    alerter.attach(context.bus)
    return alerter


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
    """CLI 的单一入口：core 与上下文共用一条连接，形状与 M3 完全一致。"""
    cfg = settings or Settings.load()
    # 自己开的连接在装配失败时自己关：调用方拿不到它，半成品的句柄会一直握着库文件
    owned = db is None
    database = db or Database.open(cfg.db_path)
    try:
        core = build_core(cfg, clock=clock, bus=bus, database=database, adapters=adapters)
        context = build_context(
            core,
            database=database,
            providers=providers,
            notifier_factory=notifier_factory,
        )
        attach_system_alerter(context)
    except BaseException:
        if owned:
            database.close()
        raise
    return Container(
        **{field.name: getattr(context, field.name) for field in fields(context)}
    )


class FreshDbProbe:
    """`/healthz` 的库探针：每次调用新开一条连接，用完就关。

    HTTP 线程不该借任何后台线程的连接（D113）。`SELECT 1` 很便宜，而这条连接
    只活一次查询——它不会把库文件长期握在手里，也不会把自己的事务深度借给别人。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def scalar(self, sql: str) -> object:
        database = Database.open(self.settings.db_path)
        try:
            return database.scalar(sql)
        finally:
            database.close()


class FreshScheduleProbe:
    """`/healthz` 的调度读数：每次新开一条连接，问的还是那两张台账。

    `Runtime` 手里有一台 `Scheduler`，但它跑在调度线程上；HTTP 线程直呼
    `scheduler.schedule_data()` 会用调度线程那条连接（D113）。这里把同一组查询
    换成「自己的连接 + 同一个 clock」，读数因此与 `sams health` 同源。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        specs: Sequence[JobSpec],
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.settings = settings
        self.specs = tuple(specs)
        self.clock = clock

    def __call__(self) -> ScheduleData:
        database = Database.open(self.settings.db_path)
        try:
            return collect(
                runs=TaskRunRepo(database, clock=self.clock),
                heartbeats=HeartbeatRepo(database, clock=self.clock),
                users=UserRepo(database),
                specs=self.specs,
                clock=self.clock,
            )
        finally:
            database.close()


@dataclass
class ServeParts:
    """`sams serve` 的四条线程上下文与那一个 `Runtime`。

    `contexts` 的顺序就是创建顺序：调度线程、盘中线程、通知线程、维护线程。维护
    那一份只被用来建 `PruneService`（它的连接随后归维护线程独占），四份都不是
    「主上下文」；通知线程的 notify 仓库由 `NotifyWorker` 持有。
    """

    core: AppCore
    contexts: tuple[ServiceContext, ServiceContext, ServiceContext, ServiceContext]
    runtime: Runtime

    def prepare(self) -> int:
        """迁移、再灌板。顺序不能反：`load_board` 读的是 0019 的列。"""
        self.contexts[0].migrate()
        return self.contexts[1].quotes.load_board()

    def close(self) -> None:
        # 一条关不掉不该挡住其余三条：少关一条就是多泄漏一条，而第一条的异常
        # 比最后一条更能说明「为什么开始收尾」
        first: BaseException | None = None
        for context in self.contexts:
            try:
                context.close()
            except BaseException as exc:
                if first is None:
                    first = exc
        try:
            self.core.close()
        except BaseException as exc:
            if first is None:
                first = exc
        if first is not None:
            raise first


def build_serve(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    bus: EventBus | None = None,
    adapters: dict[str, BarAdapter] | None = None,
    providers: dict[str, CalendarProvider] | None = None,
    notifier_factory: Callable[[str, Mapping[str, object]], Notifier] | None = None,
) -> ServeParts:
    """`sams serve` 的装配：一条进程级 core + 四条线程上下文 + 一个运行时。

    四个上下文依序造、依序记在 `opened` 里：造到第四个时炸掉，前面几条也得关。
    同一个进程里再开一条连接不会撞锁（DuckDB 共享实例），但半成品的句柄会一直
    握着库文件——Windows 上还会挡住删除与改名。
    """
    cfg = settings or Settings.load()
    core = build_core(cfg, clock=clock, bus=bus, adapters=adapters)
    opened: list[ServiceContext] = []
    try:
        for index in range(4):
            intraday = index == 1
            opened.append(
                build_context(
                    core,
                    providers=providers,
                    notifier_factory=notifier_factory,
                    # 只有盘中那一份走队列与缓存：其余三份照旧直发（§5.5）
                    queue=core.alerts_queue if intraday else None,
                    cache=SnapshotCache(core.versions) if intraday else None,
                )
            )
        scheduler_ctx, intraday_ctx, notify_ctx, maintenance_ctx = opened
        prune = PruneService(
            maintenance_ctx.db,
            quotes=maintenance_ctx.quotes_store,
            task_runs=maintenance_ctx.task_runs,
            heartbeats=maintenance_ctx.heartbeats,
            clock=core.clock,
        )
        worker = NotifyWorker(notify_ctx.notify, core.alerts_queue)
        runtime = Runtime(
            cfg,
            scheduler=scheduler_ctx.scheduler,
            # 每次新开连接的读法：HTTP 线程不借调度线程的连接（D113）
            schedule_reading=FreshScheduleProbe(
                cfg, specs=scheduler_ctx.scheduler.jobs, clock=core.clock
            ),
            monitor=intraday_ctx.intraday,
            prune=prune,
            worker=worker,
            board=core.board,
            database=FreshDbProbe(cfg),
            clock=core.clock,
        )
        # 系统告警也走队列：relay 把 `SystemAlarm` 折成工作单，`alerter.handle`
        # 在通知线程里跑。**只挂 relay 这一次**——`alerter.attach(bus)` 会把 handle
        # 直接订阅到发布线程上，两个都挂的话同一条系统告警会变成两条
        alerter = SystemAlerter(
            alerts=notify_ctx.alerts_store,
            users=notify_ctx.users,
            sender=QueuedAlertSender(notify_ctx.notify, core.alerts_queue),
            settings=cfg,
            clock=core.clock,
        )
        SystemAlarmRelay(core.alerts_queue, alerter.handle).attach(core.bus)
        return ServeParts(
            core=core,
            contexts=(scheduler_ctx, intraday_ctx, notify_ctx, maintenance_ctx),
            runtime=runtime,
        )
    except BaseException:
        for context in opened:
            context.close()
        core.close()
        raise
