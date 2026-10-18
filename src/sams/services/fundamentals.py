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
from dataclasses import dataclass
from datetime import date
from typing import Any

import polars as pl

from sams.analysis.fundamentals import BASIS_REFUSED, MarketProfitRatio, pr
from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_code, canonical_market
from sams.datasource.registry import AdapterRegistry, Attempt
from sams.storage.repos.financials import FinancialRepo, FundingFlowRepo, ValuationRepo

logger = logging.getLogger("sams.services.fundamentals")


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


class FundamentalSyncService:
    def __init__(
        self,
        registry: AdapterRegistry,
        financials: FinancialRepo,
        valuation: ValuationRepo,
        flow: FundingFlowRepo,
    ) -> None:
        self.registry = registry
        self.financials = financials
        self.valuation = valuation
        self.flow = flow

    def sync_financials(self, market: str, code: str, start: date, end: date):
        """按报告期的财务指标（ROE、增速、股息率、披露日）。"""
        return self._sync(market, code, start, end, method="financials", repo=self.financials)

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
    ) -> FundamentalRead:
        """「截至 `as_of` 已披露的最新一期 ROE」配「`as_of` 当天或之前最近的 PE」，算出 PR。

        `as_of` 缺省取今天，且**绝不以 None 的形式往下传**：那个值一路到 SQL 就等于
        「不设时间闸门」，库里的每一期都算已披露（M2-A 那条同名的红线）。
        """
        market = canonical_market(market)
        code = canonical_code(market, code)
        until = as_of if as_of is not None else self.clock.today()
        frame, dropped = self.financials.load_as_of(market, [code], until)
        reported = _first(frame)
        valued = _first(self.valuation.latest(market, [code], until))

        if reported is None and valued is None:
            # 不是「PE 缺失」也不是「ROE 为负」：这两种下一步不同（换源 vs 给预估），
            # 而这一种的下一步是同步。让 pr() 去猜会指错方向
            ratio = MarketProfitRatio(
                None,
                BASIS_REFUSED,
                reason=(
                    f"{market} {code} 库里没有已披露的财务数据，也没有按日估值："
                    "先跑 `sams sync financials` 与 `sams sync valuation`"
                    "（有数据但披露日为空的行不会被采信）"
                ),
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
            pr=ratio,
        )


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
