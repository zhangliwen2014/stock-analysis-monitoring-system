"""个股资料（F10）的两侧：把源答复搬进两张新表，再把四块档案读成一份视图。

写侧 `F10SyncService` 与基本面那三条链同一个形状：链答复 → 幂等入库 → 把降级与
「每个源都说没有」带回调用方。两处不同：

* **概况没有窗口**（一票一行，重复执行是覆盖），方法签名里的 `start/end` 只为与链同形；
* **公告的 0 行是一次答复**，与基本面的 0 行同侧（D43）：不写行，也不把它说成
  「这只票没有公告」——那句话只能由 `confirmed_empty_by` 说。

读侧 `F10Service` 只读，并且刻意不做 PIT：概况与公告是展示用的档案，港股那几期
财务指标没有披露日（D215），所以它走 `FinancialRepo.recent`，不是第二个
`load_as_of`。选股与市赚率的已披露判断仍然只有 `FundamentalService` 那一条。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import polars as pl

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import DatasourceError, ValidationError
from sams.core.timeutil import HISTORY_START
from sams.datasource.registry import AdapterRegistry, Attempt
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.f10 import AnnouncementRepo, CompanyProfileRepo
from sams.storage.repos.financials import FinancialRepo, ValuationRepo

#: 个股页一次摆几行：最近 10 条公告、最近 6 次分红送转
ANNOUNCEMENT_LIMIT = 10
DIVIDEND_LIMIT = 6

#: 财务卡的五个报告期档位（D219）。`all` 是默认视图；其余各只看同一类报告期。
PERIOD_TYPES = ("all", "annual", "q1", "h1", "q3")
#: 档位 → 源/兜底认的中文标签
PERIOD_KINDS = {"annual": "年报", "q1": "一季报", "h1": "中报", "q3": "三季报"}
#: 每档摆几列：全部 6 期（混排），年报看 5 年，单季看 8 个同季（跨年序列）
PERIOD_LIMITS = {"all": 6, "annual": 5, "q1": 8, "h1": 8, "q3": 8}
#: 原始取数：约 8 年季报，够任何一档取满
RAW_FINANCIAL_LIMIT = 32

_PROFILE_METHOD = "company_profile"
_FINANCIAL_METHOD = "financials"
_ANNOUNCEMENT_METHOD = "announcements"


@dataclass(frozen=True)
class F10SyncSummary:
    market: str
    code: str
    #: 链上的能力标签（`公司概况` / `公告`），不在这里另列一份
    kind: str = ""
    source: str = ""
    fetched: int = 0
    written: int = 0
    degraded: bool = False
    #: 链上每个有能力的源都独立答了「查无记录」——可以说「源没有」，不能说「它没有」
    empty: bool = False
    skipped: tuple[tuple[str, str], ...] = ()
    attempts: tuple[Attempt, ...] = ()
    confirmed_empty_by: tuple[str, ...] = ()


class F10SyncService:
    def __init__(
        self,
        registry: AdapterRegistry,
        profiles: CompanyProfileRepo,
        announcements: AnnouncementRepo,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.registry = registry
        self.profiles = profiles
        self.announcements = announcements
        self.clock = clock

    def sync_profile(self, market: str, code: str) -> F10SyncSummary:
        """公司概况一票一行。窗口对源没有意义，传进去只为与链同形。"""
        return self._sync(
            market,
            code,
            HISTORY_START,
            self.clock.today(),
            method=_PROFILE_METHOD,
            repo=self.profiles,
        )

    def sync_announcements(
        self, market: str, code: str, start: date, end: date
    ) -> F10SyncSummary:
        """最近一页公告。窗口只裁剪源返回的那一页，不改变「一次最多 100 条」。"""
        return self._sync(
            market,
            code,
            start,
            end,
            method=_ANNOUNCEMENT_METHOD,
            repo=self.announcements,
        )

    def _sync(
        self, market: str, code: str, start: date, end: date, *, method: str, repo
    ) -> F10SyncSummary:
        market = canonical_market(market)
        code = canonical_code(market, code)
        result = getattr(self.registry, f"fetch_{method}")(market, code, start, end)
        if result.frame.is_empty():
            return F10SyncSummary(
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
        return F10SyncSummary(
            market=market,
            code=code,
            kind=result.kind,
            source=result.source,
            fetched=result.frame.height,
            written=repo.upsert(result.frame),
            degraded=result.degraded,
            skipped=result.skipped,
            attempts=result.attempts,
        )


@dataclass(frozen=True)
class F10Read:
    """个股页那一块「资料」的全部输入，外加「这个市场有没有源」这两句空态判据。"""

    market: str
    code: str
    as_of: date
    profile: dict[str, Any] | None = None
    #: 已按档位裁剪、报告期倒序；港股那几期的 `published_date` 是 NULL（D215）
    financials: tuple[dict[str, Any], ...] = ()
    #: 本次用哪一档报告期看数（D219）
    period_type: str = "all"
    #: 本次摆了几列——模板不再自己数
    periods: int = 0
    #: 库里有没有任何一期的财务行（哪怕当前档位是空的）：空态与「本档无数据」靠它分开
    has_financial_data: bool = False
    #: 最近一个交易日的估值（PE/PB/市值）
    valuation: dict[str, Any] | None = None
    #: 最近 10 条公告，日期倒序；正文与 PDF 只留外链（D216）
    announcements: tuple[dict[str, Any], ...] = ()
    #: 最近 6 次分红送转（除权日 <= 今天），新的在前
    dividends: tuple[dict[str, Any], ...] = ()
    #: 空态分诊：链上没有这个能力时页面说「没有源」，而不是摆一张空表（D214/D106）
    has_profile_source: bool = False
    has_financials_source: bool = False
    has_announcement_source: bool = False


class F10Service:
    def __init__(
        self,
        registry: AdapterRegistry,
        profiles: CompanyProfileRepo,
        announcements: AnnouncementRepo,
        financials: FinancialRepo,
        valuation: ValuationRepo,
        corp_actions: CorpActionRepo,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.registry = registry
        self.profiles = profiles
        self.announcements = announcements
        self.financials = financials
        self.valuation = valuation
        self.corp_actions = corp_actions
        self.clock = clock

    def read(self, market: str, code: str, *, period_type: str = "all") -> F10Read:
        market = canonical_market(market)
        code = canonical_code(market, code)
        today = self.clock.today()
        profile = _first(self.profiles.get(market, code))
        valued = _first(self.valuation.latest(market, [code], today))
        actions = self.corp_actions.list_for(market, code, until=today).to_dicts()
        raw = self.financials.recent(market, code, limit=RAW_FINANCIAL_LIMIT).to_dicts()
        picked = _period_rows(raw, period_type)
        return F10Read(
            market=market,
            code=code,
            as_of=today,
            profile=profile,
            financials=picked,
            period_type=period_type,
            periods=len(picked),
            has_financial_data=bool(raw),
            valuation=valued,
            announcements=tuple(
                self.announcements.latest(market, code, limit=ANNOUNCEMENT_LIMIT).to_dicts()
            ),
            # 升序读出来，摆到屏幕上新的在前；未来的除权日不进来（与 PIT 同一条规矩）
            dividends=tuple(reversed(actions[-DIVIDEND_LIMIT:])),
            has_profile_source=self._has(market, _PROFILE_METHOD),
            has_financials_source=self._has(market, _FINANCIAL_METHOD),
            has_announcement_source=self._has(market, _ANNOUNCEMENT_METHOD),
        )

    def _has(self, market: str, method: str) -> bool:
        try:
            return bool(self.registry.chain_for(market, method))
        except DatasourceError:
            # 整个市场一个源都没配：与「缺这一个能力」对页面的答案是同一句
            return False


def _first(frame: pl.DataFrame) -> dict[str, Any] | None:
    if frame.is_empty():
        return None
    return frame.to_dicts()[0]


def _report_kind(row: dict[str, Any]) -> str | None:
    """一期报表属于哪一类：先认源给的标签，老行按报告期月份兜底（D219）。

    月份兜底只认 3/6/9/12 四个标准报告期；别的月份返回 None，不属于任何档位。
    """
    kind = row.get("report_type")
    if kind in PERIOD_KINDS.values():
        return kind
    report_date = row.get("report_date")
    month = getattr(report_date, "month", None)
    return {3: "一季报", 6: "中报", 9: "三季报", 12: "年报"}.get(month)


def _period_rows(
    rows: list[dict[str, Any]], period_type: str
) -> tuple[dict[str, Any], ...]:
    """按档位裁列：入参是报告期倒序，输出仍倒序、最多 `PERIOD_LIMITS[period_type]` 期。"""
    if period_type not in PERIOD_TYPES:
        raise ValidationError(
            f"报告期档位只认 {' / '.join(PERIOD_TYPES)}，收到 {period_type!r}"
        )
    if period_type == "all":
        picked = rows
    else:
        wanted = PERIOD_KINDS[period_type]
        picked = [row for row in rows if _report_kind(row) == wanted]
    # 每一行带着自己的档名出去：表头要写「中报/一季报/年报」，图上刻度和 tooltip 要「2025年报」
    return tuple(
        {**row, "period_kind": _report_kind(row)}
        for row in picked[: PERIOD_LIMITS[period_type]]
    )
