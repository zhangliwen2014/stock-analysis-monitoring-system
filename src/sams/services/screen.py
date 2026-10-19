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

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import polars as pl

from sams.analysis.fundamentals import BASIS_PB_ESTIMATE, BASIS_PE, pr
from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_code, canonical_market
from sams.core.fundamentals import (
    ROE_BASIS,
    ROE_BASIS_ANNUAL,
    ROE_BASIS_LATEST,
    STALE_VALUATION_DAYS,
)
from sams.storage.repos.financials import FinancialRepo, ValuationRepo
from sams.storage.repos.scan_results import ScanResultRepo
from sams.storage.repos.symbols import SymbolRepo

#: 排除原因的词表。打印方按它分组计数，所以这些句子是**数据**不是文案：
#: 两处各写一份的代价是「排除 3 只：原因 A」与「排除 3 只：原因 A（另一种写法）」并存
NO_ANNUAL_ROE = "只披露到季报/半年报，没有已披露的年报可用（要用它：`--roe-basis latest`）"
PB_BRANCH_EXCLUDED = (
    "PE 不为正，走的是现值 PB 那一支（`PB ÷ ROE ÷ ROE ÷ 100`，ROE 用已披露那期）："
    "两条公式算出的 PR 不可放在同一篮子里排序，默认不与其他票同列"
    "（要用它：`--include-pb-branch`）"
)

#: 两条公式算出的 PR 不可同列排序，所以先按支分组、支内再按值升序
_BASIS_ORDER = {BASIS_PE: 0, BASIS_PB_ESTIMATE: 1}


def no_usable_period(market: str, code: str, state: dict, until: date) -> str:
    """「库里同步过这只票，但截至这天没有一期可用」——按库的性状说清下一步。

    「同步了却一期都用不上」有两种缺法，前者的下一步是重采、后者是换截至日（D61 + D64）；
    与 `NO_ANNUAL_ROE` 的分工是：那条说「有已披露的期，只是不是年报」。
    """
    gaps = []
    if state["unpublished"]:
        gaps.append(f"{state['unpublished']} 期的披露日是空的（D61：不知何时公开即不采信）")
    next_day = state["next_published"]
    if next_day is not None:
        gaps.append(f"最早的一期 {next_day} 才公开，那天之前它在 PIT 上不存在")
    step = (
        f"要按公开那天算：`--as-of {next_day}`"
        if next_day is not None
        else f"先重跑 `sams sync financials --market {market} --code {code}`"
    )
    return (
        f"截至 {until} 没有已披露的一期可用"
        f"（库里那 {state['periods']} 期：{'；'.join(gaps)}）——{step}"
    )


#: 「下一步命令」必须当场能跑：`sams sync valuation` 缺 --market/--code 会直接报错，
#: 于是那句提示成了一条自己敲不通的命令（M2-A 第六轮栽过的同一件事）
def no_valuation(market: str, code: str) -> str:
    return f"没有按日估值（先 `sams sync valuation --market {market} --code {code}`）"


@dataclass(frozen=True)
class PrHit:
    """一只通过门槛的票。口径与两个日期跟着每一行走（D59、D60、D62）。

    `estimated` 说的是「分母是不是手填的预估」，不是「走的是 PB 那一支」：`screen pr`
    不传预估 ROE，所以那一支下它恒为 False，用的仍是已披露那期的 ROE。
    """

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
    #: `--codes` 里点了名、但库里 `financials` 一行都没有的代码（D64）。
    #: 它们不属于三堆里的任何一堆：universe 是「库里有行的票」，硬塞进 excluded 会让
    #: 「hits + priced_out + excluded == universe」这条对账等式不成立
    unmatched: list[str] = field(default_factory=list)
    #: 进了篮子的 PB 那一支有几只（D59：它与非它混排不可比，屏幕上必须出声）
    pb_branch_count: int = 0
    #: 算得出 PR、只是高于门槛的票数。落选与排除是两件事：前者是「不便宜」，后者是「不知道」
    priced_out: int = 0
    dropped_unpublished: int = 0
    stale_within_days: int = STALE_VALUATION_DAYS


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
        include_pb_branch: bool = False,
        stale_after_days: int = STALE_VALUATION_DAYS,
        record: bool = True,
    ) -> PrScan:
        """`PR ≤ threshold` 的一篮子，按 PR 升序；算不出 PR 的票逐个点名。

        批量入口各一次查询（`synced_codes` / `load_as_of` / `latest` / `publication_state`），
        不在循环里逐只问库：一次扫描问 5000 次是慢，问 4 次是可用。
        """
        if roe_basis not in ROE_BASIS:
            raise ValueError(f"ROE 口径只认 {' 或 '.join(ROE_BASIS)}，收到 {roe_basis!r}")
        market = canonical_market(market)
        until = as_of if as_of is not None else self.clock.today()
        # universe = 库里同步过基本面的全部代码，不带披露日、不带口径（D64 + D66）：
        # 一只披露日全为 NULL 的票按「有已披露一期」划会静默从范围那句话里消失，
        # 而它需要的恰好是「我们不知道它何时公开」这句点名，不是「这只票没同步」
        synced = self.financials.synced_codes(market)
        universe = synced
        unmatched: list[str] = []
        if codes is not None:
            keep = {canonical_code(market, c) for c in codes}
            present = set(synced)
            universe = [c for c in synced if c in keep]
            unmatched = sorted(keep - present)
        strategy_id = _strategy_id(threshold, roe_basis, include_pb_branch)
        market_size = self.symbols.list_market(market).height or None
        common = {
            "market": market,
            "as_of": until,
            "threshold": threshold,
            "roe_basis": roe_basis,
            "strategy_id": strategy_id,
            "universe_size": len(universe),
            "market_size": market_size,
            "unmatched": unmatched,
            "stale_within_days": stale_after_days,
        }
        if not universe:
            return PrScan(hits=[], excluded=[], **common)

        reported, dropped = self.financials.load_as_of(market, universe, until, basis=roe_basis)
        newest = self._newest_periods(market, universe, until, roe_basis)
        valued = self.valuation.latest(market, universe, until)
        by_report, by_newest, by_value = _index(reported), _index(newest), _index(valued)
        # 只在「没有可用期」时才用到的那一句：为什么没有。一次 group-by 问全 universe，
        # 不在循环里逐只问库（下面那只票的排除原因要说出具体的日期）
        by_state = _index(self.financials.publication_state(market, universe, until))

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
                by_state[code],
                threshold=threshold,
                until=until,
                stale_after_days=stale_after_days,
                include_pb_branch=include_pb_branch,
            )
            if outcome is PRICED_OUT:
                priced_out += 1
            elif isinstance(outcome, PrExclusion):
                excluded.append(outcome)
            else:
                hits.append(outcome)
        # 先按支分组（PE 那一支在前）、支内按值升序：两条公式算出的 0.06 与 0.3
        # 不是同一个量的大小，混排会让「最便宜的在第一位」这句话只对组内成立
        hits.sort(key=lambda h: (_BASIS_ORDER[h.basis], round(h.value, 6), h.code))
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
            pb_branch_count=sum(1 for hit in hits if hit.basis == BASIS_PB_ESTIMATE),
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
    state: dict,
    *,
    threshold: float,
    until: date,
    stale_after_days: int,
    include_pb_branch: bool,
) -> PrHit | PrExclusion | object:
    if reported is None:
        # 两种「这一口径下没有可用的期」，下一步不同：有更新的期 ≠ 一期都没披露（D61）
        if newest is None:
            return PrExclusion(code, no_usable_period(market, code, state, until))
        period = f"{newest['report_date']} 不是年报："
        return PrExclusion(code, f"{period}{NO_ANNUAL_ROE}")
    if valued is None:
        return PrExclusion(code, no_valuation(market, code))
    if valued["date"] < until - timedelta(days=stale_after_days):
        return PrExclusion(
            code,
            f"最近的按日估值停在 {valued['date']}，比截至日 {until} 旧过 {stale_after_days} 天："
            "那只票可能停牌，而拿旧 PE 算出的 PR 不属于今天这一篮子",
        )
    # 这里不传 `--roe-estimate`：选股要的是「这只票自己的已披露 ROE 配现值 PB」，
    # 拿一个手填的数排进一篮子，那个篮子量的就是我的猜测而不是它的便宜
    ratio = pr(pe_ttm=valued["pe_ttm"], pb=valued["pb"], roe=reported["roe"])
    if not ratio.ok:
        # D59：拒绝不是 0，也不是一条通用文案——原因由算式那一层给，这里只搬运
        return PrExclusion(code, ratio.reason)
    if ratio.basis == BASIS_PB_ESTIMATE and not include_pb_branch:
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


def _strategy_id(threshold: float, roe_basis: str, include_pb_branch: bool) -> str:
    """规则的身份：门槛、分母口径、以及有没有把 PB 那一支混进来。

    三者任一不同，产出的就是不同的一篮子；共用一个 id 会让后一次覆盖前一次，
    而「今天扫出什么」变成「今天最后一次扫出什么」。
    """
    bits = [f"pr<={threshold:g}", roe_basis]
    if include_pb_branch:
        bits.append("pb-branch")
    return "|".join(bits)


def _index(frame: pl.DataFrame) -> dict[str, dict]:
    if frame.is_empty():
        return {}
    return {row["code"]: row for row in frame.to_dicts()}
