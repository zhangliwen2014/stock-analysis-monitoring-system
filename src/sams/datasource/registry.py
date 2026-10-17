"""采集路由：把「市场 → 源优先级」的配置变成实际的降级行为。

职责边界：适配器只管「外部接口 → 标准列集」，本模块只管
「该用哪个源、失败后换谁、这次算成功还是失败（健康记录）」。
入库、限速之外的业务都在 services 层。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

import polars as pl

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.config import Settings
from sams.core.errors import DatasourceError, EmptyResponse, RateLimited, TransientError
from sams.core.events import EventBus, SystemAlarm
from sams.core.sources import SourceHealthStore, SourceRecord
from sams.datasource.actions import answers_full_history, has_actions
from sams.datasource.base import BarAdapter, has_bars
from sams.datasource.ratelimit import TokenBucket, call_with_backoff
from sams.datasource.sources import build as build_adapter
from sams.datasource.sources import known

logger = logging.getLogger("sams.datasource.registry")


def default_adapters() -> dict[str, BarAdapter]:
    """把所有已注册实现都建出来，键名 = 实现名。

    这里不再维护「源名 → 构造器」的硬编码表：实现自己在模块里 `@source` 注册，
    加一个源不用碰本文件。「配置里没提到但已注册」的实现多建出来无害——
    构造适配器不发网络请求，而漏建会让配置写错名字时报「未注册」这种误导性的话。
    """
    return {kind: build_adapter(kind) for kind in known()}

#: 值得原地退避重试的错误。FieldDrift 不在其中——改版重试不会变好，只会白等 21 秒
RETRYABLE: tuple[type[BaseException], ...] = (RateLimited, TransientError, OSError, TimeoutError)

#: 所有源都回答「无数据」时返回的空帧，列集与标准列一致，调用方无需分支
EMPTY_BARS = pl.DataFrame(
    schema={
        "market": pl.Utf8,
        "code": pl.Utf8,
        "date": pl.Date,
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Float64,
        "amount": pl.Float64,
        "source": pl.Utf8,
    }
)


@dataclass(frozen=True)
class Attempt:
    source: str
    ok: bool
    #: 真实调用次数。`attempts_per_source` 是配置值：FieldDrift 一次就定性、只调 1 次，
    #: 把它抄进这里等于对调用方说不存在的事
    tries: int = 1
    error: str | None = None


class _SourceFailed(Exception):
    """内部信号：把「这个源这次实际被调了几次」带出 `_call`。"""

    def __init__(self, source: str, tries: int, error: str) -> None:
        super().__init__(error)
        self.source = source
        self.tries = tries
        self.error = error


@dataclass(frozen=True)
class FetchResult:
    market: str
    code: str
    frame: pl.DataFrame
    source: str
    attempts: tuple[Attempt, ...] = ()
    #: 链上因「没有日线能力」被跳过的源，见 `fetch_actions` 的同一约定（D38）
    skipped: tuple[tuple[str, str], ...] = ()
    #: 所有源都回答「没有数据」（停牌、区间过短），不是失败
    empty_everywhere: bool = False

    @property
    def degraded(self) -> bool:
        return len(self.attempts) > 1


@dataclass(frozen=True)
class ActionsResult:
    """公司行为取数的结果，带「链上谁被跳过了」。

    `skipped` 不是装饰：`sams sync actions --market HK` 报「0 条」时，运维必须能一眼
    分清「这只票没分过红」与「这条链上根本没有能做这件事的源」（D38 / D36）。
    """

    market: str
    code: str
    frame: pl.DataFrame
    source: str
    attempts: tuple[Attempt, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    #: 源是否说「这只票的历史我给全了」。决定覆盖标记的写法（D36）：全历史源的确认
    #: 与按区间拉的确认，对「区间之外有没有除权」的回答能力不同
    full_history: bool = False

    @property
    def degraded(self) -> bool:
        return len(self.attempts) > 1


@dataclass
class AdapterRegistry:
    """按配置的源优先级取数；主源失败自动降级，并记录每个源的最近结果。"""

    adapters: dict[str, BarAdapter]
    market_sources: dict[str, tuple[str, ...]]
    bucket: TokenBucket | None = None
    attempts_per_source: int = 3
    sleep: Callable[[float], None] = time.sleep
    #: 源健康的落库出口；不传就只在进程内记（单测与离线场景）
    store: SourceHealthStore | None = None
    records: dict[str, SourceRecord] = field(default_factory=dict)
    #: 只用于给源健康打日期标记；真正的交易日判定归 CalendarService
    clock: Clock = SYSTEM_CLOCK
    #: 连续失败到阈值时发 SystemAlarm（spec §12：监控系统监控自己）
    bus: EventBus | None = None
    alarm_after_failures: int = 3

    def __post_init__(self) -> None:
        # 反查「这个源名被路由到哪些市场」：健康页要说的是「我把它用在哪些市场」，
        # 而不是「这个实现理论上能吃几个市场」——腾讯支持三市场，但可能只被排进 A 股链
        routed: dict[str, set[str]] = {}
        for market, sources in self.market_sources.items():
            for name in sources:
                routed.setdefault(name, set()).add(market)
        for name, adapter in self.adapters.items():
            markets = routed.get(name, set()) & set(adapter.markets)
            self.records[name] = SourceRecord(
                name=name,
                markets=tuple(sorted(markets or adapter.markets)),
            )
        for market, sources in self.market_sources.items():
            unknown = [s for s in sources if s not in self.adapters]
            if unknown:
                raise DatasourceError(
                    "registry",
                    f"市场 {market} 配置了未注册的源 {unknown}，可用: {sorted(self.adapters)}",
                )
            # 源不支持该市场是配置错误，不是运行时状况：当场失败胜过之后每次采集
            # 都「跳过」却不留痕——那样健康检查看到的是「这个源从没被调用过」
            mismatched = [s for s in sources if market not in self.adapters[s].markets]
            if mismatched:
                raise DatasourceError(
                    "registry",
                    f"市场 {market} 配置了不支持它的源 {mismatched}；"
                    f"各源支持: { {n: sorted(a.markets) for n, a in self.adapters.items()} }",
                )
        if self.store is not None:
            # 上一次的失败要能被这次看到，否则「源挂了」只在挂的那一刻有人知道
            persisted = self.store.load()
            for name, record in persisted.items():
                if name in self.records:
                    self.records[name] = record

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        adapters: dict[str, BarAdapter] | None = None,
        bucket: TokenBucket | None = None,
        store: SourceHealthStore | None = None,
        bus: EventBus | None = None,
        clock: Clock = SYSTEM_CLOCK,
        sleep: Callable[[float], None] = time.sleep,
        market_sources: dict[str, tuple[str, ...]] | None = None,
    ) -> AdapterRegistry:
        """`market_sources` 给了就用它，不再从 settings 推。

        数据源链现在是库里的记录（`data_source_routes`），管理后台改完不必重启；
        settings 里的 `[markets.*].sources` 退化成「库里一条记录都没有时」的兜底。
        """
        built = adapters if adapters is not None else default_adapters()
        return cls(
            adapters=built,
            market_sources=(
                market_sources
                if market_sources is not None
                else {m: cfg.sources for m, cfg in settings.markets.items()}
            ),
            # 桶跟着注入时钟与 sleep 走：否则「429 之后退避多久」只能靠真等来测
            bucket=bucket
            if bucket is not None
            else TokenBucket(
                settings.rate_limit_per_s,
                clock=lambda: clock.now_utc().timestamp(),
                sleep=sleep,
            ),
            store=store,
            clock=clock,
            bus=bus,
            sleep=sleep,
        )

    def sources_for(self, market: str) -> tuple[str, ...]:
        try:
            return self.market_sources[market]
        except KeyError:
            raise DatasourceError(
                "registry", f"市场 {market} 没有配置数据源，已配置: {sorted(self.market_sources)}"
            ) from None

    def adapter(self, name: str) -> BarAdapter:
        try:
            return self.adapters[name]
        except KeyError:
            raise DatasourceError("registry", f"数据源 {name} 未注册") from None

    #: 两类能力各自的判据，取数与健康检查共用（D38）
    CAPABILITIES = {"daily_bars": has_bars, "corp_actions": has_actions}

    def chain_for(self, market: str, method: str) -> tuple[str, ...]:
        """链上真有这个能力的源。

        缺能力的源在 `fetch_daily`/`fetch_actions` 里被跳过，所以「这个市场有几个备源」
        必须按同一口径数：把只有公司行为的 akshare_hk 算成港股日线的备源，健康页就会在
        「HK 只剩一个源在拉价格」的时候显示一切正常。
        """
        probe = self.CAPABILITIES[method]
        return tuple(name for name in self.sources_for(market) if probe(self.adapter(name)))

    def fetch_daily(self, market: str, code: str, start: date, end: date) -> FetchResult:
        attempts: list[Attempt] = []
        skipped: list[tuple[str, str]] = []
        empties: list[str] = []
        for name in self.sources_for(market):
            if not has_bars(self.adapter(name)):
                skipped.append((name, "不提供日线"))
                logger.info("日线跳过 %s（%s %s 链上没有这个能力）", name, market, code)
                continue
            try:
                frame = self._call(name, market, code, start, end)
            except _SourceFailed as failed:
                attempts.append(Attempt(failed.source, False, failed.tries, failed.error))
                continue
            except EmptyResponse as exc:
                empties.append(name)
                attempts.append(Attempt(name, True, 1, f"无数据: {exc}"))
                continue
            self._mark_ok(name)
            attempts.append(Attempt(name, True))
            return FetchResult(market, code, frame, name, tuple(attempts), tuple(skipped))

        if empties:
            return FetchResult(
                market, code, EMPTY_BARS, empties[0],
                tuple(attempts), tuple(skipped), empty_everywhere=True,
            )
        detail = "；".join(f"{a.source}: {a.error}" for a in attempts if not a.ok)
        raise DatasourceError("registry", f"{market} {code} 所有源均失败 → {detail}")

    def fetch_actions(self, market: str, code: str, start: date, end: date) -> ActionsResult:
        """公司行为链：按同一条路由顺序走，但**只走有这个能力的源**。

        缺能力的源进 `skipped` 并在日志里说明，不是静默跳过（D38）。区别在后果上：
        静默跳过后，「HK 没有任何源能给公司行为」这件事表现为「腾讯从没分过红」，
        而裸价跨除权日算出来的涨跌是一个看起来完全正常的数。
        """
        attempts: list[Attempt] = []
        skipped: list[tuple[str, str]] = []
        for name in self.sources_for(market):
            adapter = self.adapter(name)
            if not has_actions(adapter):
                skipped.append((name, "不提供公司行为"))
                logger.info("公司行为跳过 %s（%s %s 链上没有这个能力）", name, market, code)
                continue
            try:
                frame = self._call(name, market, code, start, end, method="corp_actions")
            except _SourceFailed as failed:
                attempts.append(Attempt(failed.source, False, failed.tries, failed.error))
                continue
            except EmptyResponse as exc:
                # 公司行为的「无数据」不能当成功：覆盖标记（D36）会把「没查过」写成「查过
                # 且没有」。所以它只是这个源答不上来，换下一个；全都答不上来就整体失败。
                attempts.append(Attempt(name, False, 1, f"无数据: {exc}"))
                logger.info("公司行为 %s 回答无数据（%s %s），换源", name, market, code)
                continue
            self._mark_ok(name)
            attempts.append(Attempt(name, True))
            return ActionsResult(
                market,
                code,
                frame,
                name,
                tuple(attempts),
                tuple(skipped),
                answers_full_history(adapter),
            )

        detail = "；".join(f"{a.source}: {a.error}" for a in attempts if not a.ok)
        raise DatasourceError(
            "registry",
            f"{market} {code} 取不到公司行为。链 {list(self.sources_for(market))} 里"
            f"被跳过的源 {skipped or []}；真调用过又失败的 {detail or '（无）'}",
        )

    def _call(
        self,
        name: str,
        market: str,
        code: str,
        start: date,
        end: date,
        *,
        method: str = "daily_bars",
    ) -> pl.DataFrame:
        adapter = self.adapter(name)
        invoke_bound = getattr(adapter, method)
        if self.bucket is not None:
            self.bucket.acquire()

        calls = 0

        def invoke() -> pl.DataFrame:
            nonlocal calls
            calls += 1
            return invoke_bound(market, code, start, end)

        try:
            # FieldDrift / EmptyResponse 不在 RETRYABLE 里：一次就定性，立刻换源
            frame = call_with_backoff(
                invoke,
                attempts=self.attempts_per_source,
                retryable=RETRYABLE,
                sleep=self.sleep,
                label=f"{name}:{market}/{code}",
            )
        except EmptyResponse:
            # 源正常应答了，只是这个区间没有数据（停牌、新股、区间过短）。
            # 记成失败会让几只停牌票就烧掉一个源的预算，还把「无数据」和「挂了」混成一条告警；
            # 也不标成功：全空本身是可疑状况，不该抹掉之前的故障记录。
            raise
        except DatasourceError as exc:
            self._mark_failure(name, exc)
            raise _SourceFailed(name, calls, str(exc)) from exc
        except BaseException as exc:  # noqa: BLE001 — 适配器漏归类时兜底，路由不能被打断
            wrapped = DatasourceError(name, f"{type(exc).__name__}: {exc}")
            self._mark_failure(name, wrapped)
            raise _SourceFailed(name, calls, str(wrapped)) from exc
        return frame if isinstance(frame, pl.DataFrame) else pl.DataFrame(frame)

    def _mark_ok(self, name: str) -> None:
        self._remember(
            SourceRecord(
                name=name,
                markets=self.records[name].markets,
                ok=True,
                last_ok_at=self.clock.today(),
            )
        )

    def _mark_failure(self, name: str, exc: DatasourceError) -> None:
        previous = self.records[name]
        self._remember(
            SourceRecord(
                name=name,
                markets=previous.markets,
                ok=False,
                last_error=f"{type(exc).__name__}: {exc}",
                last_ok_at=previous.last_ok_at,
                consecutive_failures=previous.consecutive_failures + 1,
            )
        )
        failures = self.records[name].consecutive_failures
        logger.warning("源 %s 连续失败 %d 次: %s", name, failures, exc)
        # 只在跨过阈值那一次发告警：之后每次失败都通知，等于没有通知
        if self.bus is not None and failures == self.alarm_after_failures:
            self.bus.publish(
                SystemAlarm(
                    component=f"datasource:{name}",
                    message=f"连续 {failures} 次失败，最后错误 {exc}",
                    # 用注入时钟：告警时刻必须跟着这次运行的时间走，而不是墙钟
                    ts=self.clock.now_utc(),
                )
            )

    def _remember(self, record: SourceRecord) -> None:
        self.records[record.name] = record
        if self.store is not None:
            self.store.save(record)

    def health(self) -> list[SourceRecord]:
        return [self.records[name] for name in sorted(self.records)]

