"""采集路由：把「市场 → 源优先级」的配置变成实际的降级行为。

职责边界：适配器只管「外部接口 → 标准列集」，本模块只管
「该用哪个源、失败后换谁、这次算成功还是失败（健康记录）」。
入库、限速之外的业务都在 services 层。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from math import ceil

import polars as pl

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.config import Settings
from sams.core.errors import DatasourceError, EmptyResponse, RateLimited, TransientError
from sams.core.events import EventBus, SystemAlarm
from sams.core.sources import SourceHealthStore, SourceRecord
from sams.datasource.actions import answers_full_history, empty_actions
from sams.datasource.base import BarAdapter
from sams.datasource.capabilities import capability
from sams.datasource.f10 import empty_announcements, empty_profile
from sams.datasource.fundamentals import (
    empty_financials,
    empty_flow,
    empty_valuation,
)
from sams.datasource.fx import empty_fx, has_fx
from sams.datasource.minute import answers
from sams.datasource.ratelimit import TokenBucket, call_with_backoff
from sams.datasource.sources import build as build_adapter
from sams.datasource.sources import known
from sams.datasource.spot import SPOT_BATCH, empty_snapshot, has_market_snapshot
from sams.datasource.symbol_list import empty_symbol_list, has_symbol_list

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


def _terminal_error(
    reason: str, causes: list[Exception | None], *, no_answer: bool = False
) -> DatasourceError:
    """全链都失败时终端错误该是哪一种（D88/D104）。

    判据只有一条：**每一个真失败过的源都只抖了一下**（限流/超时/网络），才值得
    「下轮再试」；链上只要有一个改版（`FieldDrift`）、或者有源连「没有」都没答清
    （`EmptyResponse`），这次失败就要人出声——重试不会让一个缺失的字段长回来。
    """
    if causes and not no_answer and all(isinstance(cause, RETRYABLE) for cause in causes):
        return TransientError("registry", reason)
    return DatasourceError("registry", reason)


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
    """内部信号：把「这个源这次实际被调了几次」带出 `_call`。

    `cause` 是原始异常而不只是它的文字：汇率那条链要按「全链是不是都只抖了一下」决定
    这次算 `TransientError` 还是算一句永久的失败，而类型在字符串里活不下来。
    """

    def __init__(self, source: str, tries: int, error: str, cause: Exception | None = None) -> None:
        super().__init__(error)
        self.source = source
        self.tries = tries
        self.error = error
        self.cause = cause


@dataclass(frozen=True)
class _Outcome:
    """一个源在一次降级链遍历里的原始结果（见 `_walk`）。

    `kind` 只描述发生了什么，不描述它意味着什么：`no_answer` 是「接口回空/解析不了」，
    对日线算一次成功的取数（停牌）、对公司行为算「这只票还没被问清楚」（D43）——
    那句话由调用方说，不由链说。
    """

    source: str
    #: skipped 链上被跳过（没这个能力）| failed 调用失败 | no_answer 答不上来 | ok 拿到帧
    kind: str
    frame: pl.DataFrame | None = None
    error: str | None = None
    tries: int = 1
    #: 原始异常：终端错误的归类要靠类型，字符串里活不下来（`_SourceFailed.cause` 同理）
    cause: Exception | None = None


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
    #: 说「查无记录」的源，且只有当链上每个有能力的源都这么说时才有值（D36）：
    #: 一个源的空答复不能替另一个源的沉默发覆盖确认，而服务层要给每个确认各记一行
    confirmed_empty_by: tuple[str, ...] = ()

    @property
    def degraded(self) -> bool:
        return len(self.attempts) > 1


@dataclass(frozen=True)
class FrameResult:
    """基本面 / 按日估值 / 资金流三条链的返回，形状与 `ActionsResult` 同一侧（D43）。

    为什么单独立一个而不是复用 `FetchResult`：`FetchResult` 里的 `empty_everywhere`
    说的是「所有源都说这段没交易」，对日线那是一次成功的取数；这三类数据里 0 行的意思是
    「这只票在那张表里没记录」，还不能写成「这只票没有财务数据」。把两种语义塞进一个
    结果类型，服务层就会在某一次重构里把它们当成同一件事——那正是 D43 拆开的两个概念。
    """

    market: str
    code: str
    #: 哪一类取数（`基本面`/`按日估值`/`资金流`）：同一条链的失败要说清是哪条链断了
    kind: str
    frame: pl.DataFrame
    source: str
    attempts: tuple[Attempt, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    #: 链上每个有能力的源都独立答了「查无记录」时才非空（D36）
    confirmed_empty_by: tuple[str, ...] = ()

    @property
    def degraded(self) -> bool:
        return len(self.attempts) > 1


@dataclass(frozen=True)
class FxResult:
    """汇率链的返回，与 `FrameResult` 同一侧：0 行要说清是「源答了没有」还是「源没答」。

    不复用 `FrameResult`：它的必填字段是 `market`/`code`，而外汇没有市场维度——
    把 `market=""` 塞进去，每一句拒绝文案与每一行日志都会多出两个读不通的空格。
    """

    base: str
    quote: str
    frame: pl.DataFrame
    source: str
    attempts: tuple[Attempt, ...] = ()
    #: 链上没有 `fx_rate` 的源（D38）：「谁能给汇率」与健康页共用一份判据
    skipped: tuple[tuple[str, str], ...] = ()
    #: 每个有能力的源都独立答了「这段没有报价」时才非空（D36）
    confirmed_empty_by: tuple[str, ...] = ()

    @property
    def degraded(self) -> bool:
        return len(self.attempts) > 1

    @property
    def empty_everywhere(self) -> bool:
        """全链都答了「没有」，且一个都没答上——这才是可以落库的「确实没有」。"""
        return bool(self.confirmed_empty_by) and self.frame.is_empty()


@dataclass(frozen=True)
class SymbolListResult:
    """代码表链的返回（D148）：按市场路由、一次答全表，所以既没有 `code` 也没有窗口。

    `empty_everywhere` 与 `fetch_daily` 同侧语义：每个有能力的源都答了「这张表是空的」
    才攒成一句可落库的「没有」；有一个源挂了就不敢替它确认。
    """

    market: str
    source: str
    frame: pl.DataFrame
    attempts: tuple[Attempt, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    empty_everywhere: bool = False

    @property
    def degraded(self) -> bool:
        return len(self.attempts) > 1


@dataclass(frozen=True)
class MarketSnapshotResult:
    """全市场快照链的返回：按市场路由、一次答一屏，既有 `code` 也没有窗口。

    缺答的代码不在这里点名——那要看「服务层问了哪些」，所以由服务层拿请求清单去比。
    """

    market: str
    source: str
    frame: pl.DataFrame
    attempts: tuple[Attempt, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    empty_everywhere: bool = False


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

    def chain_for(self, market: str, method: str) -> tuple[str, ...]:
        """链上真有这个能力的源。

        缺能力的源在降级链里被跳过，所以「这个市场有几个备源」必须按同一口径数：
        把只有公司行为的 akshare_hk 算成港股日线的备源，健康页就会在「HK 只剩一个源在拉价格」
        的时候显示一切正常。
        """
        probe = capability(method).probe
        return tuple(name for name in self.sources_for(market) if probe(self.adapter(name)))

    def _walk(
        self,
        market: str,
        code: str,
        start: date,
        end: date,
        *,
        method: str,
        extra: dict[str, object] | None = None,
    ) -> Iterator[_Outcome]:
        """按路由顺序逐源取一次数，把每个源的结果**原样**上报，由调用方定性质。

        链只知道四件事：谁没这个能力、谁调用失败、谁正常应答但没有行、谁给了行。
        「没有行」是什么后果属于取数种类：日线的 0 行是停牌或区间过短，是一次成功的取数；
        公司行为的 0 行还不能写成「这只票没分过红」（D43）。把判定放进链里，
        第三种数据（基本面）一进来就得在链里加一个 if，而那两个 if 会开始互相打架。

        生成器在调用方 `return` 时就停止推进——链上排在命中源之后的那些**不会被调用**，
        这与收敛前逐字一致（多打一次网络请求是行为改变，不是重构）。
        成功标记也交给调用方：同样一次「0 行」，日线算成功、公司行为不算，
        链没有立场替它们决定这个源这次表现如何。
        """
        cap = capability(method)
        for name in self.sources_for(market):
            adapter = self.adapter(name)
            if not cap.probe(adapter):
                logger.info("%s 跳过 %s（%s %s 链上没有这个能力）", cap.label, name, market, code)
                yield _Outcome(name, "skipped", error=cap.lacking)
                continue
            try:
                frame = self._call(name, market, code, start, end, method=method, extra=extra)
            except _SourceFailed as failed:
                yield _Outcome(
                    name, "failed", error=failed.error, tries=failed.tries, cause=failed.cause
                )
                continue
            except EmptyResponse as exc:
                yield _Outcome(name, "no_answer", error=str(exc))
                continue
            yield _Outcome(name, "ok", frame=frame)

    def fetch_daily(self, market: str, code: str, start: date, end: date) -> FetchResult:
        return self._fetch_bars_frame(market, code, start, end, method="daily_bars")

    def fetch_index_daily(
        self, market: str, index_code: str, start: date, end: date
    ) -> FetchResult:
        """指数日线链：与日线走同一条 `_walk`——「没有」「挂了」「没这能力」
        在两张表上必须是同一套语义，否则休市看板的空与错分不清。"""
        return self._fetch_bars_frame(market, index_code, start, end, method="index_daily")

    def fetch_minute_series(self, market: str, code: str, day: date) -> FetchResult:
        """分时链：`start == end == day`。

        复用 `FetchResult` 而不是另立 spec §5.1 写的 `MinuteRead`：`attempts` /
        `skipped` / `empty_everywhere` 这三样在分时上问的是同一组问题，再造一份
        只会让「链上谁被跳过」有两套写法。已回写为「实现时订正」。
        """
        return self._fetch_bars_frame(market, code, day, day, method="minute_series")

    def fetch_minute_bars(
        self, market: str, code: str, start: date, end: date, *, scale: int, count: int
    ) -> FetchResult:
        return self._fetch_bars_frame(
            market,
            code,
            start,
            end,
            method="minute_bars",
            extra={"scale": scale, "count": count},
        )

    def _fetch_bars_frame(
        self,
        market: str,
        code: str,
        start: date,
        end: date,
        *,
        method: str,
        extra: dict[str, object] | None = None,
    ) -> FetchResult:
        # 与基本面那条 `_fetch_frame` 差一个词也是没办法：同为「0 行」，这里是「这段确实没交易」
        # 的成功取数，那里是「这张表没这只票」的待核确认（D43）——两套语义不能共用一个名字。
        attempts: list[Attempt] = []
        skipped: list[tuple[str, str]] = []
        empties: list[str] = []
        causes: list[Exception | None] = []
        for out in self._walk(market, code, start, end, method=method, extra=extra):
            if out.kind == "skipped":
                skipped.append((out.source, out.error))
                continue
            if out.kind == "failed":
                attempts.append(Attempt(out.source, False, out.tries, out.error))
                causes.append(out.cause)
                continue
            if out.kind == "no_answer":
                empties.append(out.source)
                attempts.append(Attempt(out.source, True, 1, f"无数据: {out.error}"))
                continue
            self._mark_ok(out.source)
            attempts.append(Attempt(out.source, True))
            return FetchResult(
                market, code, out.frame, out.source, tuple(attempts), tuple(skipped)
            )

        if empties:
            return FetchResult(
                market,
                code,
                EMPTY_BARS,
                empties[0],
                tuple(attempts),
                tuple(skipped),
                empty_everywhere=True,
            )
        detail = "；".join(f"{a.source}: {a.error}" for a in attempts if not a.ok)
        if not attempts:
            raise DatasourceError(
                "registry",
                f"{market} {code} 没有能取{capability(method).label}的源："
                f"链 {list(self.sources_for(market))} 里"
                f"被跳过的源 {skipped or []}",
            )
        raise _terminal_error(f"{market} {code} 所有源均失败 → {detail}", causes)

    def fetch_actions(self, market: str, code: str, start: date, end: date) -> ActionsResult:
        """公司行为链：按同一条路由顺序走，但**只走有这个能力的源**。

        缺能力的源进 `skipped` 并在日志里说明，不是静默跳过（D38）。区别在后果上：
        静默跳过后，「HK 没有任何源能给公司行为」这件事表现为「腾讯从没分过红」，
        而裸价跨除权日算出来的涨跌是一个看起来完全正常的数。

        **「这个源说没有」不是一次成功的取数**：它只说明这只票在那张表里没有记录，
        而同一只票在另一张表里可能有（实测 002450：东财回查无记录，同花顺有 8 条除权）。
        所以空答复不许终结这条链——记下谁说空、继续问，整条链都说没有才算一句可写的确认
        （`confirmed_empty_by`）。只要有源没答上来，就不给它发覆盖标记。
        """
        attempts: list[Attempt] = []
        skipped: list[tuple[str, str]] = []
        empties: list[tuple[str, bool]] = []
        unanswered: list[str] = []
        causes: list[Exception | None] = []
        no_answer = False
        for out in self._walk(market, code, start, end, method="corp_actions"):
            if out.kind == "skipped":
                skipped.append((out.source, out.error))
                continue
            if out.kind == "failed":
                attempts.append(Attempt(out.source, False, out.tries, out.error))
                causes.append(out.cause)
                unanswered.append(out.source)
                continue
            if out.kind == "no_answer":
                # 源连「没有」这句话都没说清（接口回空/解析不了）：不能替它写成查过（D36）
                attempts.append(Attempt(out.source, False, 1, f"无数据: {out.error}"))
                unanswered.append(out.source)
                no_answer = True
                logger.info(
                    "公司行为 %s 回答无数据（%s %s），换源", out.source, market, code
                )
                continue
            adapter = self.adapter(out.source)
            if out.frame.height == 0:
                empties.append((out.source, answers_full_history(adapter)))
                attempts.append(Attempt(out.source, True, 1, "查无记录"))
                logger.info(
                    "公司行为 %s 说 %s %s 查无记录，继续问下一个源", out.source, market, code
                )
                continue
            self._mark_ok(out.source)
            attempts.append(Attempt(out.source, True))
            return ActionsResult(
                market,
                code,
                out.frame,
                out.source,
                tuple(attempts),
                tuple(skipped),
                answers_full_history(adapter),
            )

        if empties and not unanswered:
            # 每个有能力的源都独立答了「没有」：这才是一句可以写成覆盖标记的答复
            return ActionsResult(
                market,
                code,
                empty_actions(),
                empties[0][0],
                tuple(attempts),
                tuple(skipped),
                full_history=all(full for _, full in empties),
                confirmed_empty_by=tuple(name for name, _ in empties),
            )

        detail = "；".join(f"{a.source}: {a.error}" for a in attempts if not a.ok)
        said_no = "、".join(name for name, _ in empties)
        reason = (
            f"{market} {code} 取不到公司行为。链 {list(self.sources_for(market))} 里"
            f"被跳过的源 {skipped or []}；真调用过又失败的 {detail or '（无）'}"
            + (f"；说查无记录的 {said_no}（因为 {('、'.join(unanswered))} 没答上来，"
               f"这些「没有」还不能写成确认）" if empties else "")
        )
        raise _terminal_error(reason, causes, no_answer=no_answer)

    def fetch_financials(self, market: str, code: str, start: date, end: date) -> FrameResult:
        """按报告期的财务指标（ROE、增速、股息率）。

        粒度是**期**：按日的 PE/PB 走 `fetch_valuation`，两者在读取层才汇合成市赚率（D60）。
        """
        return self._fetch_frame(
            market, code, start, end, method="financials", empty=empty_financials()
        )

    def fetch_valuation(self, market: str, code: str, start: date, end: date) -> FrameResult:
        """按交易日的估值倍数：市赚率公式里那个「今天的 PE」的唯一来源（D60）。"""
        return self._fetch_frame(
            market, code, start, end, method="valuation", empty=empty_valuation()
        )

    def fetch_funding_flow(self, market: str, code: str, start: date, end: date) -> FrameResult:
        """按交易日的资金流。港美股这条链上可能一个源都没有——那是要说出口的缺（D38），
        不是「0 行」：后者会被读成「这只票没有主力进过场」。"""
        return self._fetch_frame(
            market, code, start, end, method="funding_flow", empty=empty_flow()
        )

    def fetch_company_profile(
        self, market: str, code: str, start: date, end: date
    ) -> FrameResult:
        """公司概况（D214）。没有时间窗口，`start`/`end` 只为与降级链同形。

        与基本面同侧：0 行是「源说这只票没有概况记录」，可以继续问下一个源；
        全链都这么说才成为一句可落库的确认。
        """
        return self._fetch_frame(
            market, code, start, end, method="company_profile", empty=empty_profile()
        )

    def fetch_announcements(
        self, market: str, code: str, start: date, end: date
    ) -> FrameResult:
        """公告元数据（D214/D216）。窗口交给源筛，只取最近一页。"""
        return self._fetch_frame(
            market, code, start, end, method="announcements", empty=empty_announcements()
        )

    def _fetch_frame(
        self, market: str, code: str, start: date, end: date, *, method: str, empty: pl.DataFrame
    ) -> FrameResult:
        """基本面三类的同一条降级链：0 行继续问，全链都说没有才成一句确认。

        与 `fetch_actions` 同侧、与 `fetch_daily` 反侧（D43）：日线的 0 行是「这段日子确实
        没交易」，是一次成功的取数；基本面的 0 行只是「这张表里没这只票的记录」——把它当
        答复交付，服务层就写下「601088 没有财务数据」，而真相可能只是主源那天没这张表。
        走同一段循环而不是复制三份，是 T-0 收敛的全部意义：加一种数据只加一个语义判断，
        不再加三十行会各自漂移的走法。
        """
        cap = capability(method)
        attempts: list[Attempt] = []
        skipped: list[tuple[str, str]] = []
        empties: list[str] = []
        unanswered: list[str] = []
        causes: list[Exception | None] = []
        no_answer = False
        for out in self._walk(market, code, start, end, method=method):
            if out.kind == "skipped":
                skipped.append((out.source, out.error))
                continue
            if out.kind == "failed":
                attempts.append(Attempt(out.source, False, out.tries, out.error))
                causes.append(out.cause)
                unanswered.append(out.source)
                continue
            if out.kind == "no_answer":
                # 源连「没有」都没说清（接口回空、解析不出）：不能替它写成查过（D36）
                attempts.append(Attempt(out.source, False, 1, f"无数据: {out.error}"))
                unanswered.append(out.source)
                no_answer = True
                logger.info("%s %s 回答无数据（%s %s），换源", cap.label, out.source, market, code)
                continue
            if out.frame.height == 0:
                empties.append(out.source)
                attempts.append(Attempt(out.source, True, 1, "查无记录"))
                logger.info(
                    "%s %s 说 %s %s 查无记录，继续问下一个源", cap.label, out.source, market, code
                )
                continue
            self._mark_ok(out.source)
            attempts.append(Attempt(out.source, True))
            return FrameResult(
                market,
                code,
                cap.label,
                out.frame,
                out.source,
                tuple(attempts),
                tuple(skipped),
            )

        if empties and not unanswered:
            return FrameResult(
                market,
                code,
                cap.label,
                empty,
                empties[0],
                tuple(attempts),
                tuple(skipped),
                confirmed_empty_by=tuple(empties),
            )

        detail = "；".join(f"{a.source}: {a.error}" for a in attempts if not a.ok)
        said_no = "、".join(empties)
        reason = (
            f"{market} {code} 取不到{cap.label}。链 {list(self.sources_for(market))} 里"
            f"被跳过的源 {skipped or []}；真调用过又失败的 {detail or '（无）'}"
            + (
                f"；说查无记录的 {said_no}（因为 {'、'.join(unanswered)} 没答上来，"
                f"这些「没有」还不能写成确认）"
                if empties
                else ""
            )
        )
        raise _terminal_error(reason, causes, no_answer=no_answer)

    def fetch_fx(self, base: str, quote: str, start: date, end: date) -> FxResult:
        """汇率链：注册表里所有「提供汇率」的源，按注册顺序试，与 `market_sources` 无关。

        不走 `chain_for` / `_walk` 是因为外汇没有市场维度，那两个是按 `(market, code)`
        路由的；仍然记源健康、仍然在连续失败时发 SystemAlarm——「汇率取不到」的后果
        （跨币种持仓的换汇影响拆不出）与「日线取不到」同量级，不能只让服务层的 notes
        单独承担。

        一次问整段区间（问 `start` 那天，收下源给的全部行）而不是按日循环：按日循环会把
        一次限流风险变成三十次，而汇价是连续序列，逐日问的答复不会比一次问的多。

        **「源答了这段没有」是一句答复，「源挂了」不是**（D36/D43，与日线同侧）：
        只有每个有能力的源都说了没有，才攒成一次可落库的确认 `confirmed_empty_by`。
        """
        cap = capability("fx_rate")
        attempts: list[Attempt] = []
        skipped: list[tuple[str, str]] = []
        empties: list[str] = []
        failed: list[tuple[str, str, Exception | None]] = []
        for name in self.adapters:
            adapter = self.adapter(name)
            if not has_fx(adapter):
                logger.info("汇率 跳过 %s（链上没有这个能力）", name)
                skipped.append((name, cap.lacking))
                continue
            try:
                frame = self._call_fx(name, base, quote, start, end)
            except _SourceFailed as exc:
                attempts.append(Attempt(name, False, exc.tries, exc.error))
                failed.append((name, exc.error, exc.cause))
                continue
            except EmptyResponse as exc:
                attempts.append(Attempt(name, True, 1, f"无数据: {exc}"))
                empties.append(name)
                logger.info("汇率 %s 说 %s→%s 这段没有数据，换源", name, base, quote)
                continue
            self._mark_ok(name)
            attempts.append(Attempt(name, True))
            return FxResult(
                base, quote, frame, name, tuple(attempts), tuple(skipped)
            )

        if empties and not failed:
            return FxResult(
                base,
                quote,
                empty_fx(),
                empties[0],
                tuple(attempts),
                tuple(skipped),
                confirmed_empty_by=tuple(empties),
            )

        detail = "；".join(f"{name}: {error}" for name, error, _ in failed) or "（无）"
        said_no = "、".join(empties)
        reason = (
            f"取不到 {base}→{quote} 的汇率。链 {list(self.adapters)} 里"
            f"被跳过的源 {skipped or []}；真调用过又失败的 {detail}"
            + (
                f"；说这段没有数据的 {said_no}（因为 {'、'.join(n for n, _, _ in failed)} "
                f"没答上来，这些「没有」还不能写成确认）"
                if empties and failed
                else ""
            )
        )
        raise _terminal_error(reason, [cause for _, _, cause in failed])

    def fetch_symbol_list(self, market: str) -> SymbolListResult:
        """代码表链：与 `fetch_fx` 同一套归类，差别只在**按市场路由**（代码表有市场维度）。

        一次答全表、没有窗口，所以没有 `_call_fx` 的日期裁剪段；仍然记源健康、
        仍然在连续失败时发 SystemAlarm（D148）。「全链都说这张表是空的」才是
        `empty_everywhere`，掺进一次失败就出声——把空表写进仓库等于删掉现有清单。
        """
        cap = capability("symbol_list")
        attempts: list[Attempt] = []
        skipped: list[tuple[str, str]] = []
        empties: list[str] = []
        failed: list[tuple[str, str, Exception | None]] = []
        for name in self.sources_for(market):
            adapter = self.adapter(name)
            if not has_symbol_list(adapter):
                logger.info("代码表 跳过 %s（%s 链上没有这个能力）", name, market)
                skipped.append((name, cap.lacking))
                continue
            try:
                frame = self._call_symbol_list(name, market)
            except _SourceFailed as exc:
                attempts.append(Attempt(name, False, exc.tries, exc.error))
                failed.append((name, exc.error, exc.cause))
                continue
            except EmptyResponse as exc:
                attempts.append(Attempt(name, True, 1, f"无数据: {exc}"))
                empties.append(name)
                logger.info("代码表 %s 说 %s 这张表是空的，换源", name, market)
                continue
            self._mark_ok(name)
            attempts.append(Attempt(name, True))
            return SymbolListResult(market, name, frame, tuple(attempts), tuple(skipped))

        if empties and not failed:
            return SymbolListResult(
                market,
                empties[0],
                empty_symbol_list(),
                tuple(attempts),
                tuple(skipped),
                empty_everywhere=True,
            )

        detail = "；".join(f"{name}: {error}" for name, error, _ in failed) or "（无）"
        if not attempts:
            raise DatasourceError(
                "registry",
                f"{market} 没有能取代码表的源：链 {list(self.sources_for(market))}",
            )
        causes = [cause for _, _, cause in failed]
        raise _terminal_error(f"{market} 所有源均取代码表失败 → {detail}", causes)

    def fetch_market_snapshot(self, market: str, codes: list[str]) -> MarketSnapshotResult:
        """快照链：一次答一个市场，按市场路由、不按票重试（D173）。

        「这个源答不答这个市场」看意愿表（`answers`），与目录、置灰共用同一份判据
        （D165）——链上挂着 `market_snapshot` 方法不代表它愿意答港美股。
        """
        cap = capability("market_snapshot")
        attempts: list[Attempt] = []
        skipped: list[tuple[str, str]] = []
        empties: list[str] = []
        failed: list[tuple[str, str, Exception | None]] = []
        for name in self.sources_for(market):
            adapter = self.adapter(name)
            if not has_market_snapshot(adapter):
                logger.info("快照 跳过 %s（%s 链上没有这个能力）", name, market)
                skipped.append((name, cap.lacking))
                continue
            if not answers(adapter, "market_snapshot", market):
                logger.info("快照 跳过 %s（不供 %s）", name, market)
                skipped.append((name, f"不供{market}"))
                continue
            try:
                frame = self._call_market_snapshot(name, market, codes)
            except _SourceFailed as exc:
                attempts.append(Attempt(name, False, exc.tries, exc.error))
                failed.append((name, exc.error, exc.cause))
                continue
            except EmptyResponse as exc:
                attempts.append(Attempt(name, True, 1, f"无数据: {exc}"))
                empties.append(name)
                logger.info("快照 %s 说 %s 这张表是空的，换源", name, market)
                continue
            self._mark_ok(name)
            attempts.append(Attempt(name, True))
            return MarketSnapshotResult(market, name, frame, tuple(attempts), tuple(skipped))

        if empties and not failed:
            return MarketSnapshotResult(
                market,
                empties[0],
                empty_snapshot(),
                tuple(attempts),
                tuple(skipped),
                empty_everywhere=True,
            )

        detail = "；".join(f"{name}: {error}" for name, error, _ in failed) or "（无）"
        if not attempts:
            # 把每个源的跳过原因写进话里：只报「链上没有」会让人以为是配错了源名，
            # 而实际可能是腾讯不愿答这个市场（D165/D188）
            why = "；".join(f"{name}: {reason}" for name, reason in skipped) or "（无）"
            raise DatasourceError(
                "registry",
                f"{market} 没有能取全市场快照的源：链 {list(self.sources_for(market))}；"
                f"跳过 {why}",
            )
        causes = [cause for _, _, cause in failed]
        raise _terminal_error(f"{market} 所有源均取快照失败 → {detail}", causes)

    def _call_symbol_list(self, name: str, market: str) -> pl.DataFrame:
        """对单个源的一次代码表取数：限速、退避、归类，逐行照抄 `_call_fx`，只删日期裁剪。"""
        adapter = self.adapter(name)
        if self.bucket is not None:
            self.bucket.acquire()
        calls = 0

        def invoke() -> pl.DataFrame:
            nonlocal calls
            calls += 1
            return adapter.symbol_list(market)

        try:
            frame = call_with_backoff(
                invoke,
                attempts=self.attempts_per_source,
                retryable=RETRYABLE,
                sleep=self.sleep,
                label=f"{name}:symbol_list {market}",
            )
        except EmptyResponse:
            raise
        except DatasourceError as exc:
            self._mark_failure(name, exc)
            raise _SourceFailed(name, calls, str(exc), exc) from exc
        except Exception as exc:  # noqa: BLE001 — 适配器漏归类时兜底，路由不能被打断
            wrapped = DatasourceError(name, f"{type(exc).__name__}: {exc}")
            self._mark_failure(name, wrapped)
            raise _SourceFailed(name, calls, str(wrapped), exc) from exc
        out = frame if isinstance(frame, pl.DataFrame) else pl.DataFrame(frame)
        if out.is_empty():
            # 一个 0 行的帧与一次 `EmptyResponse` 是同一句话：这张表没内容，继续问下一个源
            raise EmptyResponse(name, f"{market} 代码表返回 0 行")
        return out

    def _call_market_snapshot(self, name: str, market: str, codes: list[str]) -> pl.DataFrame:
        """对单个源的一次快照取数：限速、退避、归类，与 `_call_symbol_list` 同一形状。

        一次方法内部会切成多片 HTTP 请求（批大小是接口的硬边界），所以限速桶按片数
        领令牌——只领一次等于把真实请求量报小了一个数量级。
        """
        adapter = self.adapter(name)
        if self.bucket is not None:
            for _ in range(max(1, ceil(len(codes) / SPOT_BATCH))):
                self.bucket.acquire()
        calls = 0

        def invoke() -> pl.DataFrame:
            nonlocal calls
            calls += 1
            return adapter.market_snapshot(market, codes)

        try:
            frame = call_with_backoff(
                invoke,
                attempts=self.attempts_per_source,
                retryable=RETRYABLE,
                sleep=self.sleep,
                label=f"{name}:market_snapshot {market}",
            )
        except EmptyResponse:
            raise
        except DatasourceError as exc:
            self._mark_failure(name, exc)
            raise _SourceFailed(name, calls, str(exc), exc) from exc
        except Exception as exc:  # noqa: BLE001
            wrapped = DatasourceError(name, f"{type(exc).__name__}: {exc}")
            self._mark_failure(name, wrapped)
            raise _SourceFailed(name, calls, str(wrapped), exc) from exc
        out = frame if isinstance(frame, pl.DataFrame) else pl.DataFrame(frame)
        if out.is_empty():
            raise EmptyResponse(name, f"{market} 快照返回 0 行")
        return out

    def _call_fx(self, name: str, base: str, quote: str, start: date, end: date) -> pl.DataFrame:
        """对单个源的一次汇率取数：限速、退避、归类，与 `_call` 同一套预算。

        不复用 `_call` 是因为它按 `(market, code)` 组织调用；这里刻意把失败归类、
        源健康标记与「0 行继续问」那套语义原样抄一遍形状，让两条链对同一个源的
        判定不会分叉。
        """
        adapter = self.adapter(name)
        if self.bucket is not None:
            self.bucket.acquire()
        calls = 0

        def invoke() -> pl.DataFrame:
            nonlocal calls
            calls += 1
            return adapter.fx_rate(base, quote, start, end)

        try:
            frame = call_with_backoff(
                invoke,
                attempts=self.attempts_per_source,
                retryable=RETRYABLE,
                sleep=self.sleep,
                label=f"{name}:fx {base}→{quote}",
            )
        except EmptyResponse:
            raise
        except DatasourceError as exc:
            self._mark_failure(name, exc)
            raise _SourceFailed(name, calls, str(exc), exc) from exc
        except Exception as exc:  # noqa: BLE001 — 适配器漏归类时兜底，路由不能被打断
            wrapped = DatasourceError(name, f"{type(exc).__name__}: {exc}")
            self._mark_failure(name, wrapped)
            raise _SourceFailed(name, calls, str(wrapped), exc) from exc
        out = frame if isinstance(frame, pl.DataFrame) else pl.DataFrame(frame)
        if out.is_empty():
            # 一个 0 行的帧与一次 `EmptyResponse` 是同一句话：这段没有。
            # 让它走进「拿到了数据」那条分支，降级链就会替一句该继续问的空答复背书
            raise EmptyResponse(name, f"{base}→{quote} 在 {start}~{end} 返回 0 行")
        # 源的窗口比我们问的宽（Yahoo 给 `start` 前后几天）：请求说到的区间就是承诺，
        # 多出来的日子落进库会让人以为 `--end` 那天之前真的都同步过
        trimmed = out.filter(pl.col("date").is_between(start, end))
        if trimmed.is_empty():
            raise EmptyResponse(name, f"{base}→{quote} 给的行都在 {start}~{end} 之外")
        return trimmed

    def _call(
        self,
        name: str,
        market: str,
        code: str,
        start: date,
        end: date,
        *,
        method: str = "daily_bars",
        extra: dict[str, object] | None = None,
    ) -> pl.DataFrame:
        adapter = self.adapter(name)
        invoke_bound = getattr(adapter, method)
        if self.bucket is not None:
            self.bucket.acquire()

        calls = 0

        def invoke() -> pl.DataFrame:
            nonlocal calls
            calls += 1
            return invoke_bound(market, code, start, end, **(extra or {}))

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
            raise _SourceFailed(name, calls, str(exc), exc) from exc
        except Exception as exc:  # noqa: BLE001 — 适配器漏归类时兜底，路由不能被打断
            # 不是 BaseException：Ctrl-C 不是「这个源坏了，换下一个」，
            # 兜住它等于让中断要等整条链每个源都试完才生效，还把一次取消记成源故障
            wrapped = DatasourceError(name, f"{type(exc).__name__}: {exc}")
            self._mark_failure(name, wrapped)
            raise _SourceFailed(name, calls, str(wrapped), exc) from exc
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

