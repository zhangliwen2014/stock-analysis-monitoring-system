"""选股：把「按市赚率挑票」这条规则跑在一个可说明的范围上。

这条命令的输入不是「全市场」，而是**库里同步过基本面的那些票**。这两个数差着三四个
数量级（A 股 5000 多只，逐个采集要按小时计），所以扫描的第一句话必须是范围，
不是结果——「今天一只都没有」在 137 只的样本上与在 5000 只的样本上完全是两件事，
而前者的正确下一步是再去同步几只，不是得出「市场不便宜」的结论。

第二种静默比第一种更贵：**没有 PR 的票**。`PR ≤ 0.4` 想找的正是困境反转与周期底部，
而那一类票恰恰最容易算不出 PR（PE 为负、只披露到季报、估值行停在两个月前）。
把它们静默丢掉，结果看起来完整，实则把这条规则最该抓的东西筛掉了。
所以每一只没进结果的票都带一个原因出门，原因里带着下一步的命令（D43、D59）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta

import polars as pl

from sams.analysis.fundamentals import BASIS_PB_ESTIMATE, pr
from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_code, canonical_market
from sams.core.fundamentals import ROE_BASIS, ROE_BASIS_ANNUAL, ROE_BASIS_LATEST
from sams.storage.repos.financials import FinancialRepo, ValuationRepo
from sams.storage.repos.scan_results import ScanResultRepo
from sams.storage.repos.symbols import SymbolRepo

#: 排除原因的词表。打印方按它分组计数，所以这些句子是**数据**不是文案：
#: 两处各写一份的代价是「排除 3 只：原因 A」与「排除 3 只：原因 A（另一种写法）」并存
NO_ANNUAL_ROE = "只披露到季报/半年报，没有已披露的年报可用（要用它：`--roe-basis latest`）"
PB_BRANCH_EXCLUDED = (
    "PE 不为正，那一支的 PR 里嵌了预估 ROE：只与**同一个预估**下的数可比，"
    "默认不与其他票同列排序（要用它：`--include-estimated`）"
)


#: 「下一步命令」必须当场能跑：`sams sync valuation` 缺 --market/--code 会直接报错，
#: 于是那句提示成了一条自己敲不通的命令（M2-A 第六轮栽过的同一件事）
def no_valuation(market: str, code: str) -> str:
    return f"没有按日估值（先 `sams sync valuation --market {market} --code {code}`）"


@dataclass(frozen=True)
class PrHit:
    """一只通过门槛的票。口径与两个日期跟着每一行走（D59、D60、D62）。"""

    code: str
    #: 与门槛比较的是 `round(value, 6)`：门槛写在命令行上，就要能被正好敲中
    value: float
    basis: str
    roe_used: float | None
    pe_ttm: float | None
    pb: float | None
    #: 分母那一期（年报口径下是一个 12-31）
    period: date | None
    #: 分子那一天的估值日——与报告期不是同一个粒度
    valuation_on: date | None
    estimated: bool


@dataclass(frozen=True)
class PrExclusion:
    """一只没进结果的票，带它自己的原因。"""

    code: str
    reason: str


@dataclass(frozen=True)
class PrScan:
    market: str
    as_of: date
    threshold: float
    #: 分母口径（D62）：跨口径的 PR 不可同列排序，所以它是这条规则身份的一部分
    roe_basis: str
    #: 落库存档用的规则标识：同 (日期, 市场, 这个 id) 重跑覆盖
    strategy_id: str
    #: 实际被扫的票数——第一句诚实话的那个数字
    universe_size: int
    #: 这个市场主档里在市的票数；主档为空时 None（不知道就说不知道，不写 0）
    market_size: int | None
    hits: list[PrHit]
    excluded: list[PrExclusion]
    #: 算得出 PR、只是高于门槛的票数。落选与排除是两件事：前者是「不便宜」，后者是「不知道」
    priced_out: int = 0
    dropped_unpublished: int = 0
    stale_within_days: int = 7


#: 「算得出但高于门槛」这个事实不需要带数据出门，一个单例就够
PRICED_OUT = object()


class ScreenService:
    def __init__(
        self,
        financials: FinancialRepo,
        valuation: ValuationRepo,
        symbols: SymbolRepo,
        scans: ScanResultRepo,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.financials = financials
        self.valuation = valuation
        self.symbols = symbols
        self.scans = scans
        self.clock = clock

    def pr_max(
        self,
        market: str,
        threshold: float,
        *,
        as_of: date | None = None,
        codes: list[str] | None = None,
        roe_basis: str = ROE_BASIS_ANNUAL,
        include_estimated: bool = False,
        stale_after_days: int = 7,
        record: bool = True,
    ) -> PrScan:
        """`PR ≤ threshold` 的一篮子，按 PR 升序；算不出 PR 的票逐个点名。

        批量入口各一次查询（`covered_codes` / `load_as_of` / `latest`），不在循环里
        逐只问库：一次扫描问 5000 次是慢，问 3 次是可用。
        """
        if roe_basis not in ROE_BASIS:
            raise ValueError(f"ROE 口径只认 {' 或 '.join(ROE_BASIS)}，收到 {roe_basis!r}")
        market = canonical_market(market)
        until = as_of if as_of is not None else self.clock.today()
        # universe 按「有没有已披露的一期」划，不带上口径：一只只有季报的票确实在库里，
        # 年报口径下算不出 PR 是**要被点名的排除**，不是「这只票不在样本里」
        universe = self.financials.covered_codes(market, as_of=until, basis=ROE_BASIS_LATEST)
        if codes is not None:
            keep = {canonical_code(market, c) for c in codes}
            universe = [c for c in universe if c in keep]
        strategy_id = _strategy_id(threshold, roe_basis, include_estimated)
        market_size = self.symbols.list_market(market).height or None
        common = {
            "market": market,
            "as_of": until,
            "threshold": threshold,
            "roe_basis": roe_basis,
            "strategy_id": strategy_id,
            "universe_size": len(universe),
            "market_size": market_size,
            "stale_within_days": stale_after_days,
        }
        if not universe:
            return PrScan(hits=[], excluded=[], **common)

        reported, dropped = self.financials.load_as_of(market, universe, until, basis=roe_basis)
        newest = self._newest_periods(market, universe, until, roe_basis)
        valued = self.valuation.latest(market, universe, until)
        by_report, by_newest, by_value = _index(reported), _index(newest), _index(valued)

        hits: list[PrHit] = []
        excluded: list[PrExclusion] = []
        priced_out = 0
        for code in universe:
            outcome = _one(
                market,
                code,
                by_report.get(code),
                by_newest.get(code),
                by_value.get(code),
                threshold=threshold,
                until=until,
                stale_after_days=stale_after_days,
                include_estimated=include_estimated,
            )
            if outcome is PRICED_OUT:
                priced_out += 1
            elif isinstance(outcome, PrExclusion):
                excluded.append(outcome)
            else:
                hits.append(outcome)
        hits.sort(key=lambda h: (round(h.value, 6), h.code))
        excluded.sort(key=lambda e: e.code)
        if record:
            self.scans.record(
                on=until,
                market=market,
                strategy_id=strategy_id,
                universe_size=len(universe),
                hits=[asdict(hit) for hit in hits],
            )
        return PrScan(
            hits=hits,
            excluded=excluded,
            priced_out=priced_out,
            dropped_unpublished=dropped,
            **common,
        )

    def _newest_periods(
        self, market: str, universe: list[str], until: date, roe_basis: str
    ) -> pl.DataFrame:
        """年报口径下顺手问一次「最新已披露到哪一期」：排除那句要说出具体的期。"""
        if roe_basis == ROE_BASIS_LATEST:
            return pl.DataFrame()
        return self.financials.load_as_of(market, universe, until, basis=ROE_BASIS_LATEST)[0]


def _one(
    market: str,
    code: str,
    reported: dict | None,
    newest: dict | None,
    valued: dict | None,
    *,
    threshold: float,
    until: date,
    stale_after_days: int,
    include_estimated: bool,
) -> PrHit | PrExclusion | object:
    if reported is None:
        period = "" if newest is None else f"{newest['report_date']} 不是年报："
        return PrExclusion(code, f"{period}{NO_ANNUAL_ROE}")
    if valued is None:
        return PrExclusion(code, no_valuation(market, code))
    if valued["date"] < until - timedelta(days=stale_after_days):
        return PrExclusion(
            code,
            f"最近的按日估值停在 {valued['date']}，比截至日 {until} 旧过 {stale_after_days} 天："
            "那只票可能停牌，而拿旧 PE 算出的 PR 不属于今天这一篮子",
        )
    ratio = pr(pe_ttm=valued["pe_ttm"], pb=valued["pb"], roe=reported["roe"])
    if not ratio.ok:
        # D59：拒绝不是 0，也不是一条通用文案——原因由算式那一层给，这里只搬运
        return PrExclusion(code, ratio.reason)
    if ratio.basis == BASIS_PB_ESTIMATE and not include_estimated:
        return PrExclusion(code, PB_BRANCH_EXCLUDED)
    if round(ratio.value, 6) > round(threshold, 6):
        return PRICED_OUT
    return PrHit(
        code=code,
        value=ratio.value,
        basis=ratio.basis,
        roe_used=ratio.roe_used,
        pe_ttm=valued["pe_ttm"],
        pb=valued["pb"],
        period=reported["report_date"],
        valuation_on=valued["date"],
        estimated=ratio.estimated,
    )


def _strategy_id(threshold: float, roe_basis: str, include_estimated: bool) -> str:
    """规则的身份：门槛、分母口径、以及有没有把预估那一支混进来。

    三者任一不同，产出的就是不同的一篮子；共用一个 id 会让后一次覆盖前一次，
    而「今天扫出什么」变成「今天最后一次扫出什么」。
    """
    bits = [f"pr<={threshold:g}", roe_basis]
    if include_estimated:
        bits.append("with-estimate")
    return "|".join(bits)


def _index(frame: pl.DataFrame) -> dict[str, dict]:
    if frame.is_empty():
        return {}
    return {row["code"]: row for row in frame.to_dicts()}
