"""基本面、按日估值、资金流的同步与读取用例（D58–D61 在服务层的落点）。

两个类各管一侧，中间隔着「库里有没有」这条线：

* `FundamentalSyncService` 只把链上的答复搬进库。它不解释数据，也不在链断的时候
  写半行——半次成功写成一次成功，之后所有「查无财务数据」的结论就建立在这次沉默上。
* `FundamentalService` 只读，并且是 PR 的**输入决定者**：`analysis.fundamentals`
  那两条公式要的是「今天的 PE」和「最近已披露那期的 ROE」（D60、D59），而哪个数
  算「今天已披露」由这里的 `as_of` 与 `published_date` 说了算。分层守卫（analysis
  不许 import storage）正是为了让这句话只在一个地方说。

三类数据的走法相同、语义不同，这份不同写在 `empty` 那一栏：日线的 0 行是一次成功的
取数（那段确实没交易），基本面的 0 行只是「这几家都没这只票的记录」（D43）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

import polars as pl

from sams.analysis.fundamentals import BASIS_REFUSED, MarketProfitRatio, pr
from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_code, canonical_market
from sams.core.fundamentals import ROE_BASIS_ANNUAL, ROE_BASIS_LATEST
from sams.datasource.registry import AdapterRegistry, Attempt
from sams.storage.repos.financials import FinancialRepo, FundingFlowRepo, ValuationRepo
from sams.storage.repos.symbols import SymbolRepo

logger = logging.getLogger("sams.services.fundamentals")

#: 上市日期那一格问的是哪条能力链（D38 的那份判据，不在这里另列谁能给）
_LISTED_KIND = "listed_date"
#: 三种「这一格还是空的」，各自的下一步不同（与 `_missing_roe_reason` 同一规矩）
NO_MASTER_ROW = (
    "上市日期没填上：主档里还没有 {market} {code} 这一行，无处可写——"
    "先 `sams sync daily --market {market} --code {code} --start … --end …`"
)
NO_CAPABLE_SOURCE = "上市日期没填上：{market} 的链上没有能给上市日期的源"
NO_ANSWER = "上市日期没填上：{detail}"


@dataclass(frozen=True)
class FundamentalSyncSummary:
    market: str
    code: str
    #: 哪一类数据（`基本面`/`按日估值`/`资金流`），由链上的能力标签带出来，不在这里再列一份
    kind: str = ""
    source: str = ""
    fetched: int = 0
    written: int = 0
    degraded: bool = False
    #: 链上每个有能力的源都独立答了「查无记录」——可以说「这几家都没有」，不能说「它没有」
    empty: bool = False
    skipped: tuple[tuple[str, str], ...] = ()
    attempts: tuple[Attempt, ...] = ()
    confirmed_empty_by: tuple[str, ...] = ()
    #: 这次**填上**的上市日（U18）。库里本来就有值时这里是 None：没填就不说「填了」
    listed_on: date | None = None
    #: 没填上时的原因与下一步；填上了就是空串
    listed_on_note: str = ""


class FundamentalSyncService:
    def __init__(
        self,
        registry: AdapterRegistry,
        financials: FinancialRepo,
        valuation: ValuationRepo,
        flow: FundingFlowRepo,
        symbols: SymbolRepo,
    ) -> None:
        self.registry = registry
        self.financials = financials
        self.valuation = valuation
        self.flow = flow
        #: 上市日期落在主档那一格（U18）：没有这个仓库，D37 的豁免就只能靠人每次手传
        self.symbols = symbols

    def sync_financials(self, market: str, code: str, start: date, end: date):
        """按报告期的财务指标（ROE、增速、股息率、披露日）。

        顺手把上市日期也填一次（U18）：这一问挂在基本面这条链上而不是新加一条命令，
        因为漏敲一条命令的后果是「豁免静默不生效」，而屏幕上看不出来。
        """
        summary = self._sync(market, code, start, end, method="financials", repo=self.financials)
        if summary.empty:
            # 「这几家都没有这只票的财务记录」时不去问上市日：同一个源的答复已经在了
            return summary
        listed_on, note = self._fill_listed_on(summary.market, summary.code)
        return replace(summary, listed_on=listed_on, listed_on_note=note)

    def sync_valuation(self, market: str, code: str, start: date, end: date):
        """按交易日的估值倍数——PR 公式里那个「今天的 PE」的唯一来源（D60）。"""
        return self._sync(market, code, start, end, method="valuation", repo=self.valuation)

    def sync_flow(self, market: str, code: str, start: date, end: date):
        """按交易日的资金流。港美股这条链上可能一个源都没有，那是 D38 要出声的缺。"""
        return self._sync(market, code, start, end, method="funding_flow", repo=self.flow)

    def _sync(self, market, code, start, end, *, method: str, repo) -> FundamentalSyncSummary:
        """三条链共用的一段：链答复 → 幂等入库 → 把降级与跳过带回调用方。

        键在入口归一（D50）：`hk`/`700` 直接问下去，链上记的健康、库里存的行、屏幕上读
        的会是三种写法，而 `valuation_daily` 与 `financials` 一旦分键，PR 就配不上对。
        """
        market = canonical_market(market)
        code = canonical_code(market, code)
        result = getattr(self.registry, f"fetch_{method}")(market, code, start, end)
        if result.frame.is_empty():
            # 0 行时链已经问过每个有能力的源，这里一行都不写：写一行空记录，
            # 下次读取就会把「我们没查到」当成「它没有」来回答
            return FundamentalSyncSummary(
                market=market,
                code=code,
                kind=result.kind,
                source=result.source,
                degraded=result.degraded,
                empty=True,
                skipped=result.skipped,
                attempts=result.attempts,
                confirmed_empty_by=result.confirmed_empty_by,
            )
        written = repo.upsert(result.frame)
        logger.info(
            "%s %s %s %d 行（源 %s）", market, code, result.kind, written, result.source
        )
        return FundamentalSyncSummary(
            market=market,
            code=code,
            kind=result.kind,
            source=result.source,
            fetched=result.frame.height,
            written=written,
            degraded=result.degraded,
            skipped=result.skipped,
            attempts=result.attempts,
        )

    def _fill_listed_on(self, market: str, code: str) -> tuple[date | None, str]:
        """问一次上市日、填进主档那一格，返回「填上了哪天」与「没填上时怎么说」。

        三处刻意的设计：

        * **已有值就不问**（`SymbolRepo.fill_listed_on` 同一条规矩的读侧）：换一家源就可能
          换一天，而 D37 的豁免窗口跟着这个日子走。
        * **不走降级链的机器**（`registry._walk`）：这一问没有窗口、也不该记进源健康——
          上市日期取不到不是「akshare 挂了」，把它算进去会让一次主档元数据的失败
          烧掉那只票真正要用的那条链的预算。用的仍是同一份能力判据（`chain_for`）。
        * **任何答复都不打断已经到手的基本面入库**：它是主档的一格，不是那条链的一半。
        """
        symbol = self.symbols.get(market, code)
        if symbol is None:
            return None, NO_MASTER_ROW.format(market=market, code=code)
        if symbol.get("listed_on") is not None:
            return None, ""
        chain = self.registry.chain_for(market, _LISTED_KIND)
        if not chain:
            return None, NO_CAPABLE_SOURCE.format(market=market)
        said_no: list[str] = []
        failed: list[str] = []
        for name in chain:
            try:
                day = self.registry.adapter(name).listed_date(market, code)
            except Exception as exc:  # noqa: BLE001 — 这一问不能让一次成功的同步变成失败
                failed.append(f"{name} 报错（{type(exc).__name__}: {exc}）")
                continue
            if day is None:
                said_no.append(f"{name} 说查无记录")
                continue
            self.symbols.fill_listed_on(market, code, day)
            logger.info("%s %s 上市日期 %s（源 %s）", market, code, day, name)
            return day, ""
        return None, NO_ANSWER.format(detail="；".join([*said_no, *failed]) or "（无）")


@dataclass(frozen=True)
class FundamentalRead:
    """一次 PR 所需的全部输入，外加「这些数分别来自哪一天、哪一家」。"""

    market: str
    code: str
    as_of: date
    #: 被采信的那一期报告期；库里没有已披露数据时为 None
    period: date | None = None
    published_on: date | None = None
    roe: float | None = None
    revenue_yoy: float | None = None
    profit_yoy: float | None = None
    dividend_yield: float | None = None
    pe_ttm: float | None = None
    pb: float | None = None
    market_cap: float | None = None
    #: PE/PB 是**哪一天**的数（D60：与报告期不是一个粒度，不能混为一谈）
    valuation_on: date | None = None
    #: 这个数来自哪一家（D54：数的出处要跟着数一起出门）
    source: str = ""
    valuation_source: str = ""
    #: D61：因没有披露日而未被采信的行数——「我们不知道它何时公开」不等于「它不存在」
    dropped_unpublished: int = 0
    #: D62：这一期的 ROE 是按哪个口径被采信的（年报 / 最新一期）
    roe_basis: str = ROE_BASIS_ANNUAL
    #: 年报口径下，比被采信那期**更新**的一期已披露报表（周期不同，故未用于 PR）
    interim_period: date | None = None
    interim_roe: float | None = None
    interim_published_on: date | None = None
    pr: MarketProfitRatio = MarketProfitRatio(None, BASIS_REFUSED, reason="未计算")


class FundamentalService:
    def __init__(
        self,
        financials: FinancialRepo,
        valuation: ValuationRepo,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.financials = financials
        self.valuation = valuation
        self.clock = clock

    def read(
        self,
        market: str,
        code: str,
        *,
        as_of: date | None = None,
        roe_estimate: float | None = None,
        roe_basis: str = ROE_BASIS_ANNUAL,
    ) -> FundamentalRead:
        """「截至 `as_of` 已披露的最新一期 ROE」配「`as_of` 当天或之前最近的 PE」，算出 PR。

        `as_of` 缺省取今天，且**绝不以 None 的形式往下传**：那个值一路到 SQL 就等于
        「不设时间闸门」，库里的每一期都算已披露（M2-A 那条同名的红线）。

        `roe_basis`（D62）默认只认年报：源的 ROE 是该期累计值，而 PE 是滚动十二个月，
        按「最新已披露一期」时同一个 PE 会因最近报的是 Q1 还是 H1 而给出差数倍的 PR。
        选 `latest` 仍然算数，但那个 PR 只与同口径的 PR 可比——这句话由调用方打印。
        """
        market = canonical_market(market)
        code = canonical_code(market, code)
        until = as_of if as_of is not None else self.clock.today()
        frame, dropped = self.financials.load_as_of(market, [code], until, basis=roe_basis)
        reported = _first(frame)
        valued = _first(self.valuation.latest(market, [code], until))
        # 年报口径下顺手问一次「最新一期是哪天」：那只票可能刚报了半年数。不说这句，
        # 读的人会以为我们不知道有更新的一期——而我们知道，只是那个数的周期不同
        newest = (
            None
            if roe_basis == ROE_BASIS_LATEST
            else _first(
                self.financials.load_as_of(market, [code], until, basis=ROE_BASIS_LATEST)[0]
            )
        )
        interim = _newer_than_adopted(newest, reported)

        if reported is None:
            # 三种「算不出」的下一步各不相同，所以分开说（M2-A 的拒绝分诊同一条规矩）
            ratio = MarketProfitRatio(
                None, BASIS_REFUSED, reason=_missing_roe_reason(
                    market, code, until, valued, interim, roe_basis
                )
            )
        else:
            ratio = pr(
                pe_ttm=_get(valued, "pe_ttm"),
                pb=_get(valued, "pb"),
                roe=_get(reported, "roe"),
                roe_estimate=roe_estimate,
            )
        return FundamentalRead(
            market=market,
            code=code,
            as_of=until,
            period=_get(reported, "report_date"),
            published_on=_get(reported, "published_date"),
            roe=_get(reported, "roe"),
            revenue_yoy=_get(reported, "revenue_yoy"),
            profit_yoy=_get(reported, "profit_yoy"),
            dividend_yield=_get(reported, "dividend_yield"),
            pe_ttm=_get(valued, "pe_ttm"),
            pb=_get(valued, "pb"),
            # 市值是某一天的数：优先按日那一行，只有它没有时才回落到报告期那一列
            market_cap=_pick(_get(valued, "market_cap"), _get(reported, "market_cap")),
            valuation_on=_get(valued, "date"),
            source=_get(reported, "source") or "",
            valuation_source=_get(valued, "source") or "",
            dropped_unpublished=dropped,
            roe_basis=roe_basis,
            interim_period=_get(interim, "report_date"),
            interim_roe=_get(interim, "roe"),
            interim_published_on=_get(interim, "published_date"),
            pr=ratio,
        )


def _missing_roe_reason(
    market: str, code: str, until: date, valued: dict | None, interim: dict | None, basis: str
) -> str:
    """PR 算不出，但「为什么」有三种，各自的下一步命令不同（D62 + D43）。"""
    if valued is None and interim is None:
        return (
            f"{market} {code} 库里截至 {until} 没有已披露的财务数据，也没有按日估值："
            "先跑 `sams sync financials` 与 `sams sync valuation`"
            "（有数据但披露日为空的行不会被采信）"
        )
    if interim is not None:
        return (
            f"{market} {code} 截至 {until} 只披露到 {interim['report_date']}（"
            f"{ROE_BASIS_LATEST} 口径下的一期），没有已披露的**年报**可用："
            "年报口径算不出 PR——传 `--roe-basis latest` 可以用那一期，"
            "但那里的 ROE 是该期累计值、与 PE(TTM) 的十二个月不同周期"
        )
    return (
        f"{market} {code} 截至 {until} 库里没有已披露的财务数据"
        f"（口径 {basis}）：先跑 `sams sync financials`，"
        "披露日为空的行不会被采信"
    )


def _newer_than_adopted(newest: dict | None, adopted: dict | None) -> dict | None:
    """比被采信那期**更新**的一期，没有则 None。

    年报口径下这是必须说的一句话：库里有 2026-06-30 的半年数而我们用的是 2025-12-31 的
    年度数，不写出来就像没看见更新的那期。
    """
    if newest is None:
        return None
    if adopted is None:
        return newest
    return newest if newest["report_date"] > adopted["report_date"] else None


def _first(frame: pl.DataFrame) -> dict[str, Any] | None:
    if frame.is_empty():
        return None
    return frame.to_dicts()[0]


def _get(row: dict[str, Any] | None, column: str) -> Any:
    return None if row is None else row.get(column)


def _pick(*values):
    """第一个**不是 None** 的值：0.0 是一个数，不能被当成「没有」跳过。"""
    for value in values:
        if value is not None:
            return value
    return None
