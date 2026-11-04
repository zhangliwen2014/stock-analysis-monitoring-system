"""决策卡 v0（S1-A）：把一份估值读数折成「此刻该不该动手」。

这一层不算估值本身：合理区间、止损方法、目标候选都来自 `services/valuation` 的
`ValuationRead`。它只做四件事，每一件都有一个静默出错的口子：

* **先过闸，再说话**。日线不足、报价超期、财务超期时只出「数据不足，不出结论」；
  拿半个计划给一个赔率，比不给结论更糟。
* **买入口径与持仓口径分开**。模板里的 pct 止损按现价算，现价高于买入区时它会落在
  买入区之上；买入口径必须按买入区中值重算，否则赔率的分母是负的。持仓口径才用
  按现价算的那一档，它是这笔仓位真正的失效价。
* **成本与止损距离进公式**。不扣成本、不判「止损过近」，赔率就是一张自证纸。
* **证伪优先于价格**。价格在买入区 + 假设被证伪，输出不追/退出，绝不输出可分批。

结论只有七个词：未持仓 `不追 / 等价格 / 证据不足 / 可分批建仓`，持仓
`持有 / 减仓 / 退出`。胜率与仓位不在这一版（样本不足，n<30 恒成立）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from sams.analysis.valuation.compose import stop_level
from sams.analysis.valuation.styles import Style
from sams.core.assumptions import REFUTED
from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import NotAdjustable
from sams.services.valuation import ValuationRead, ValuationService
from sams.storage.repos.assumptions import Assumption, AssumptionRepo
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.ledger import Account, Position, SimAccountRepo, SimOrderRepo
from sams.storage.repos.theses import ARCHIVED, INVALIDATED, Thesis, ThesisRepo
from sams.storage.repos.watchlists import WatchlistRepo

#: 买卖合计的保守成本（spec §11.1）：A 0.3% / HK 0.6% / US 0.2%
COST_PCT = {"A": 0.003, "HK": 0.006, "US": 0.002}
#: 报价超期的交易日数：A/HK 3 个交易日、US 2 个
QUOTE_MAX_AGE = {"A": 3, "HK": 3, "US": 2}
#: 财务超期的日历日：A 120 天 / HK 200 / US 100
FINANCIAL_MAX_AGE = {"A": 120, "HK": 200, "US": 100}
#: 日线样本地板（spec §11.1）：不足这个数就不出结论
MIN_BARS = 300
#: 分批建仓的赔率下限：`R_low >= 2`
STARTER_MIN_R = 2.0
#: 止损距离的最小比例：`max(3%, 1×ATR14)`
MIN_STOP_DISTANCE = 0.03
#: 这版卡的方法标签：落进 thesis 的 `plan_version`，以后换口径时旧行还认得出
PLAN_VERSION = "decision-v0"

#: 结论词表。未持仓四态、持仓三态，没有「买入」这种祈使句（spec §5）
NOT_CHASE = "不追"
WAIT = "等价格"
INSUFFICIENT = "证据不足"
STARTER = "可分批建仓"
HOLD = "持有"
REDUCE = "减仓"
EXIT = "退出"


@dataclass(frozen=True)
class CardQuote:
    """页面交给卡片的报价那一格（`web.quotes.QuoteView` 的摘录）。

    服务层不认识 web 那个读模型；它只要六个字段就能说「这个数是哪天的、哪来的」。
    """

    price: float | None
    day: date | None
    source: str = ""
    kind: str = "close"
    state: str = ""
    label: str = ""
    detail: str = ""


@dataclass(frozen=True)
class Holding:
    """同一只票在一个账户里的那一笔。多账户时各算各的，合计留给屏幕。"""

    account_id: str
    account_name: str
    qty: float
    avg_cost: float


@dataclass(frozen=True)
class Evidence:
    """卡底那三条证据之一：说的是数从哪条链来，不是把数再念一遍。"""

    label: str
    text: str


@dataclass(frozen=True)
class DecisionCard:
    market: str
    code: str
    as_of: date
    #: 三道硬闸全过才为 True；False 时止损/目标/赔率一律留空
    data_ok: bool
    #: 港美这一版只有降级卡：没有估值区间，也没有 R
    degraded: bool
    #: 没过的那几道闸，各自一句为什么
    gates: tuple[str, ...]
    quote: CardQuote
    held: tuple[Holding, ...]
    watched: bool
    thesis: Thesis | None
    refuted: tuple[Assumption, ...]
    fair_low: float | None
    fair_high: float | None
    #: 「低于合理下限 32.9%」——现价相对价值区间的位置
    price_vs_fair: str
    entry_low: float | None
    entry_high: float | None
    entry_ready: bool | None
    stop: float | None
    stop_method: str
    stop_distance_pct: float | None
    #: 目标区间的两端（合理上限、前高），`target_used` 是计入 R 的那一端
    target_low: float | None
    target_high: float | None
    target_used: float | None
    odds: OddsRange
    conclusion: str
    reason: str
    evidence: tuple[Evidence, ...]
    plan_version: str = PLAN_VERSION

    @property
    def quantity(self) -> float:
        return sum(item.qty for item in self.held)


@dataclass(frozen=True)
class OddsRange:
    """赔率区间：两端都扣成本；无效时 `low/high` 都是 None，`reason` 说为什么。"""

    low: float | None
    high: float | None
    reason: str = ""
    evidence: str = ""

    @property
    def valid(self) -> bool:
        return self.low is not None and self.high is not None


def odds_range(
    *,
    t_low: float | None,
    p_low: float | None,
    p_high: float | None,
    stop: float | None,
    cost_pct: float,
    reference: float | None,
    atr: float | None,
) -> OddsRange:
    """`R_low=(T−P_high−c)/(P_high−S+c)`，`R_high` 用 `P_low`（spec §11.1）。

    `reference` 是判「止损过近」的那把尺：买入口径用买入区中值，持仓口径用现价。
    止损距离小于 `max(3% × reference, 1×ATR14)` 时分母接近 0，赔率会被算成一个
    好看的大数——这一条必须在这里拒，不能留给屏幕去解释。
    """
    missing = [
        name
        for name, value in (
            ("目标", t_low),
            ("买入区下限", p_low),
            ("买入区上限", p_high),
            ("止损", stop),
        )
        if value is None
    ]
    if missing:
        return OddsRange(None, None, f"赔率缺 {'、'.join(missing)}：少一个数就不是同一个赔率")
    assert t_low is not None and p_low is not None and p_high is not None and stop is not None
    if not (0 < stop < p_low <= p_high):
        return OddsRange(
            None,
            None,
            f"止损 {stop:g} 不在买入区 {p_low:g}~{p_high:g} 之下："
            "买入区的价格还没到失效价，R 的分母不为正",
        )
    if t_low <= p_high:
        return OddsRange(
            None,
            None,
            f"目标 {t_low:g} 不高于买入区上限 {p_high:g}：这条计划没有向上的那一段",
        )
    if reference is None or reference <= 0:
        return OddsRange(None, None, "没有参考价，判不了止损距离")
    distance = reference - stop
    floor = max(MIN_STOP_DISTANCE * reference, atr or 0.0)
    if distance < floor:
        return OddsRange(
            None,
            None,
            f"止损过近，R 无效：距参考价 {reference:g} 只有 {distance:g}，"
            f"不到 max(3%, ATR14) = {floor:g}",
        )
    cost_low = cost_pct * p_low
    cost_high = cost_pct * p_high
    low = (t_low - p_high - cost_high) / (p_high - stop + cost_high)
    high = (t_low - p_low - cost_low) / (p_low - stop + cost_low)
    if low <= 0:
        return OddsRange(None, None, f"扣完成本后 R_low={low:.2f} 不大于 0：这笔交易不成立")
    low, high = sorted((low, high))
    return OddsRange(
        low,
        high,
        evidence=(
            f"R_low=(目标 {t_low:g} − 买入上限 {p_high:g} − 成本 {cost_high:.4g}) ÷ "
            f"(买入上限 {p_high:g} − 止损 {stop:g} + 成本 {cost_high:.4g}) = {low:.2f}；"
            f"R_high 用买入下限 {p_low:g} = {high:.2f}；成本 {cost_pct:.2%}"
        ),
    )


def trading_days_between(
    calendar,
    market: str,
    start: date | None,
    until: date | None,
    *,
    limit: int | None = None,
) -> int:
    """`start` 之后到 `until`（含）之间有几个交易日。

    日历能回答时听日历；回答不了的那一天按工作日保守计——宁可早一点判报价超期，
    不可晚判。`limit` 只用来早停：闸门只问「有没有超过 N 天」，不必把十年数完。
    """
    if start is None or until is None or until <= start:
        return 0
    count = 0
    day = start
    while day < until:
        day = date.fromordinal(day.toordinal() + 1)
        status = calendar.known_status(market, day)
        if status is True or (status is None and day.weekday() < 5):
            count += 1
            if limit is not None and count > limit:
                return count
    return count


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:g}"


def _label_rows(labels: Iterable[str]) -> str:
    return "、".join(labels)


class DecisionService:
    """一张卡答一个账户的一个问题：此刻该不该动手。"""

    def __init__(
        self,
        valuation: ValuationService,
        bars: BarRepo,
        calendar,
        accounts: SimAccountRepo,
        orders: SimOrderRepo,
        watchlists: WatchlistRepo,
        theses: ThesisRepo,
        assumptions: AssumptionRepo,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.valuation = valuation
        self.bars = bars
        self.calendar = calendar
        self.accounts = accounts
        self.orders = orders
        self.watchlists = watchlists
        self.theses = theses
        self.assumptions = assumptions
        self.clock = clock

    def card(
        self,
        user_id: str,
        market: str,
        code: str,
        quote: CardQuote,
        *,
        style: Style | str = "value",
    ) -> DecisionCard:
        market = canonical_market(market)
        code = canonical_code(market, code)
        as_of = self.clock.today()
        held = self._held(user_id, market, code)
        thesis = self._active_thesis(user_id, market, code)
        refuted = (
            tuple(
                item
                for item in self.assumptions.list_for(thesis.id)
                if item.status == REFUTED
            )
            if thesis is not None
            else ()
        )
        watched = self.watchlists.contains(user_id, market, code)
        degraded = market != "A"

        bars = self.bars.count(market, code)
        gates: list[str] = []
        if bars < MIN_BARS:
            gates.append(f"日线只有 {bars} 根（至少要 {MIN_BARS} 根）")
        gates.extend(self._quote_gates(market, quote, as_of))

        shell = dict(
            market=market,
            code=code,
            as_of=as_of,
            data_ok=False,
            degraded=degraded,
            gates=tuple(gates),
            quote=quote,
            held=held,
            watched=watched,
            thesis=thesis,
            refuted=refuted,
        )

        if degraded:
            return self._degraded(shell)
        if bars < MIN_BARS:
            return self._insufficient(shell, reason="数据不足，不出结论：" + "；".join(gates))

        # 复权闸（`require_coverage` / 未标记跳变）拒的是「这一个数」，而这一趟里有三处会读到它：
        # 估值分位、持仓区间涨跌、入场卡的前高。任一处抛出都不该把整页拖走——专家实测里
        # 15 只蓝筹 6 只整页 400，于是卡片上那句「打开这只票的页面会把它登记进后台补采队列」
        # 自相矛盾：页面根本没打开。这里退回「证据不足」那一态，其余各卡照常渲染（D285）。
        try:
            read = self.valuation.read(market, code, as_of=as_of, style=style)
            gates.extend(self._financial_gates(read, as_of))
            shell["gates"] = tuple(gates)
            if gates:
                return self._insufficient(shell, reason="数据不足，不出结论：" + "；".join(gates))
            shell["data_ok"] = True

            if held:
                return self._position_card(shell, read, quote)
            return self._entry_card(shell, read, quote, style)
        except NotAdjustable as exc:
            reason = str(exc)
            shell["gates"] = (*gates, reason)
            return self._insufficient(shell, reason=reason)

    # --- 读账户 ---------------------------------------------------------------------------

    def _held(self, user_id: str, market: str, code: str) -> tuple[Holding, ...]:
        found: list[Holding] = []
        for account in self.accounts.list_for(user_id):
            position = self._position(account, market, code)
            if position is not None:
                found.append(
                    Holding(
                        account_id=account.id,
                        account_name=account.name,
                        qty=position.qty,
                        avg_cost=position.avg_cost,
                    )
                )
        return tuple(found)

    def _position(self, account: Account, market: str, code: str) -> Position | None:
        for position in self.orders.positions(account.id):
            if position.market == market and position.code == code and position.qty:
                return position
        return None

    def _active_thesis(self, user_id: str, market: str, code: str) -> Thesis | None:
        active = [
            item
            for item in self.theses.list(user_id)
            if item.market == market
            and item.code == code
            and item.status not in (ARCHIVED, INVALIDATED)
        ]
        if not active:
            return None
        return max(active, key=lambda item: item.updated_at or item.created_at)

    # --- 闸 -------------------------------------------------------------------------------

    def _quote_gates(self, market: str, quote: CardQuote, as_of: date) -> list[str]:
        if quote.price is None or quote.day is None:
            return ["没有能用的报价：拿不到现价就不会有结论"]
        limit = QUOTE_MAX_AGE[market]
        age = trading_days_between(self.calendar, market, quote.day, as_of, limit=limit)
        if age > limit:
            return [
                f"报价停在 {quote.day}，距 {as_of} 已 {age} 个交易日"
                f"（上限 {limit} 个）"
            ]
        return []

    def _financial_gates(self, read: ValuationRead, as_of: date) -> list[str]:
        fundamental = read.fundamental
        fresh = max(
            (
                day
                for day in (fundamental.published_on, fundamental.interim_published_on)
                if day is not None
            ),
            default=None,
        )
        if fresh is None:
            return ["没有已披露的财报：财务闸过不了，PR 与合理区间都没有分母"]
        age = (as_of - fresh).days
        limit = FINANCIAL_MAX_AGE["A"]
        if age > limit:
            return [
                f"最新已披露财报停在 {fresh}，距 {as_of} 已 {age} 天（上限 {limit} 天）"
            ]
        return []

    # --- 卡片 -----------------------------------------------------------------------------

    def _degraded(self, shell: dict) -> DecisionCard:
        """港美降级卡：现价 + 论点里的失效价/目标，不假装有估值链。"""
        thesis = shell["thesis"]
        stop = None if thesis is None else thesis.stop_price
        target = None if thesis is None else thesis.target_price
        price = shell["quote"].price
        held = shell["held"]
        refuted = shell["refuted"]
        odds = OddsRange(None, None, "缺估值链：港美这一版不给估值区间与 R")
        if not shell["gates"]:
            shell["data_ok"] = True

        if not held:
            conclusion, reason = INSUFFICIENT, "港美这一版没有估值链：给不出买入区与 R，不出结论"
        elif refuted:
            conclusion, reason = EXIT, "论点的假设已证伪：先离开，再重新判断"
        elif stop is None or target is None:
            conclusion, reason = INSUFFICIENT, (
                "港美没有估值链，论点里也没有失效价/目标：不知道何时退出，不出持有结论"
            )
        elif price is None:
            conclusion, reason = INSUFFICIENT, "没有现价：判不了止损与目标"
        elif price <= stop:
            conclusion, reason = EXIT, f"现价 {price:g} 已在止损 {stop:g} 上或下方"
        elif price >= target:
            conclusion, reason = REDUCE, f"现价 {price:g} 已到目标 {target:g} 之上：先减持"
        else:
            conclusion, reason = HOLD, f"现价 {price:g} 在失效价 {stop:g} 与目标 {target:g} 之间"

        evidence = [self._price_evidence(shell["quote"])]
        if thesis is not None:
            evidence.append(
                Evidence(
                    "风险",
                    f"论点 {thesis.status} 的失效价 {_fmt(stop)}、目标 {_fmt(target)}"
                    f"（读数日 {thesis.levels_on or '手填'}）；港美没有估值链，R 留空",
                )
            )
        return DecisionCard(
            **shell,
            fair_low=None,
            fair_high=None,
            price_vs_fair="缺估值链",
            entry_low=None,
            entry_high=None,
            entry_ready=None,
            stop=stop,
            stop_method="论点",
            stop_distance_pct=self._distance(price, stop),
            target_low=target,
            target_high=target,
            target_used=target,
            odds=odds,
            conclusion=conclusion,
            reason=reason,
            evidence=tuple(evidence),
        )

    def _insufficient(self, shell: dict, *, reason: str) -> DecisionCard:
        return DecisionCard(
            **shell,
            fair_low=None,
            fair_high=None,
            price_vs_fair="—",
            entry_low=None,
            entry_high=None,
            entry_ready=None,
            stop=None,
            stop_method="",
            stop_distance_pct=None,
            target_low=None,
            target_high=None,
            target_used=None,
            odds=OddsRange(None, None, "数据不足：不出 R"),
            conclusion=INSUFFICIENT,
            reason=reason,
            evidence=(self._price_evidence(shell["quote"]),),
        )

    def _entry_card(
        self, shell: dict, read: ValuationRead, quote: CardQuote, style: Style | str
    ) -> DecisionCard:
        plan = read.plan
        entry_low, entry_high = plan.entry.low, plan.entry.high
        entry_mid = (
            None
            if entry_low is None or entry_high is None
            else (entry_low + entry_high) / 2.0
        )
        stop, stop_method = self._entry_stop(read, entry_mid)
        targets = self._targets(read, entry_mid)
        target_low = None if not targets else min(targets)
        target_high = None if not targets else max(targets)
        target_used = target_low
        refuted = shell["refuted"]

        if entry_low is None or entry_high is None or entry_mid is None:
            reason = plan.entry.reason or "买入区没有算出来：不知道该在哪一档定价"
            return self._insufficient(shell, reason=f"关键价位缺失：{reason}")
        if stop is None:
            reason = plan.stop.reason or "止损没有算出来：不知道失效价，R 的分母就空了"
            return self._insufficient(shell, reason=f"关键价位缺失：{reason}")
        if target_used is None:
            reason = "合理上限与前高都不高于买入区中值：向上没有参照物"
            return self._insufficient(shell, reason=f"关键价位缺失：{reason}")

        odds = odds_range(
            t_low=target_used,
            p_low=entry_low,
            p_high=entry_high,
            stop=stop,
            cost_pct=COST_PCT[shell["market"]],
            reference=entry_mid,
            atr=read.atr,
        )
        price = quote.price
        ready = self._entry_ready(read, price)

        if refuted:
            conclusion, reason = NOT_CHASE, (
                f"论点有 {len(refuted)} 条假设已证伪：证伪优先于价格，不给建仓结论"
            )
        elif price is None:
            conclusion, reason = INSUFFICIENT, "没有现价：判不了到没到买入区"
        elif price <= stop:
            conclusion, reason = NOT_CHASE, (
                f"现价 {price:g} 已在止损 {stop:g} 上或下方：这不是回踩，是信号失效"
            )
        elif price >= target_used:
            conclusion, reason = NOT_CHASE, (
                f"现价 {price:g} 已到目标下沿 {target_used:g} 之上：向上的那一段已经走完"
            )
        elif ready and odds.valid and odds.low >= STARTER_MIN_R:
            conclusion, reason = STARTER, (
                f"现价 {price:g} 已到买入区上限 {entry_high:g} 之下，"
                f"R_low {odds.low:.2f} ≥ {STARTER_MIN_R:g}"
            )
        elif ready and odds.valid:
            conclusion, reason = WAIT, (
                f"现价已到买入区，但 R_low {odds.low:.2f} 不到 {STARTER_MIN_R:g}：等更好的价格"
            )
        elif ready:
            conclusion, reason = WAIT, f"现价已到买入区，但 R 无效：{odds.reason}"
        else:
            conclusion, reason = WAIT, (
                f"现价 {price:g} 还在买入区上限 {entry_high:g} 之上：这一档等回落到 "
                f"{entry_high:g} 以下"
            )

        return DecisionCard(
            **shell,
            fair_low=plan.fair.low,
            fair_high=plan.fair.high,
            price_vs_fair=self._vs_fair(price, plan.fair.low, plan.fair.high),
            entry_low=entry_low,
            entry_high=entry_high,
            entry_ready=ready,
            stop=stop,
            stop_method=stop_method,
            stop_distance_pct=self._distance(price, stop),
            target_low=target_low,
            target_high=target_high,
            target_used=target_used,
            odds=odds,
            conclusion=conclusion,
            reason=reason,
            evidence=self._evidence(shell, read, stop, stop_method, target_used, odds),
        )

    def _position_card(
        self, shell: dict, read: ValuationRead, quote: CardQuote
    ) -> DecisionCard:
        plan = read.plan
        thesis = shell["thesis"]
        stop = plan.stop.low if plan.stop.ok else (
            None if thesis is None else thesis.stop_price
        )
        stop_method = read.style.param("stop_kind") if plan.stop.ok else "论点"
        targets = self._targets(read, None)
        target_low = None if not targets else min(targets)
        target_high = None if not targets else max(targets)
        target_used = target_low
        refuted = shell["refuted"]
        price = quote.price

        if stop is None or target_used is None:
            return self._insufficient(
                shell,
                reason="关键价位缺失：持仓口径没有止损或没有目标，不拿「持有」替一个不知道何时退出的仓位背书",
            )

        odds = (
            OddsRange(None, None, "没有现价：判不了 R")
            if price is None
            else odds_range(
                t_low=target_used,
                p_low=price,
                p_high=price,
                stop=stop,
                cost_pct=COST_PCT[shell["market"]],
                reference=price,
                atr=read.atr,
            )
        )

        if refuted:
            conclusion, reason = EXIT, "论点的假设已证伪：先离开，再重新判断"
        elif price is None:
            conclusion, reason = INSUFFICIENT, "没有现价：判不了止损与目标"
        elif price <= stop:
            conclusion, reason = EXIT, f"现价 {price:g} 已在止损 {stop:g} 上或下方"
        elif price >= target_used:
            conclusion, reason = REDUCE, (
                f"现价 {price:g} 已到目标下沿 {target_used:g} 之上：先减持，"
                "目标之后的钱不归这条计划"
            )
        else:
            conclusion, reason = HOLD, (
                f"现价 {price:g} 在失效价 {stop:g} 与目标 {target_used:g} 之间"
            )

        return DecisionCard(
            **shell,
            fair_low=plan.fair.low,
            fair_high=plan.fair.high,
            price_vs_fair=self._vs_fair(price, plan.fair.low, plan.fair.high),
            entry_low=plan.entry.low,
            entry_high=plan.entry.high,
            entry_ready=plan.entry.ready,
            stop=stop,
            stop_method=str(stop_method),
            stop_distance_pct=self._distance(price, stop),
            target_low=target_low,
            target_high=target_high,
            target_used=target_used,
            odds=odds,
            conclusion=conclusion,
            reason=reason,
            evidence=self._evidence(shell, read, stop, str(stop_method), target_used, odds),
        )

    # --- 价位的取法 -----------------------------------------------------------------------

    def _entry_stop(
        self, read: ValuationRead, entry_mid: float | None
    ) -> tuple[float | None, str]:
        """买入口径的止损：pct/atr2 按买入区中值重算，其余用计划里那一档。

        模板的 pct 止损按**现价**算；现价高于买入区时它落在买入区之上，拿它算 R 会得到
        负分母。买入口径问的是「在这个价买，跌到哪算错」，锚点因此是买入区中值。
        """
        kind = str(read.style.param("stop_kind"))
        if entry_mid is None:
            return None, kind
        if kind in ("pct", "atr2"):
            level = stop_level(
                kind=kind,
                price=entry_mid,
                stop_pct=read.style.param("stop_pct"),
                atr_multiplier=read.style.param("atr_multiplier"),
                atr=read.atr,
            )
            return (level.low, kind) if level.ok else (None, kind)
        return (read.plan.stop.low, kind) if read.plan.stop.ok else (None, kind)

    def _entry_ready(self, read: ValuationRead, price: float | None) -> bool | None:
        """「到没到买入区」按**报价**判，不按估值那根日线收盘判。

        估值读数的 `plan.entry.ready` 比的是它自己那根日线（可能是昨天的收盘）；
        卡片上现价那一格来自报价。两者在盘中能差好几个百分点，用错了就会在
        13 元的报价旁写「还没到 13.58 的买入区」。
        """
        low, high = read.plan.entry.low, read.plan.entry.high
        if price is None or low is None or high is None:
            return None
        if read.style.param("entry_rule") == "weekly_ma20_pullback":
            return low <= price <= high
        return price <= high

    def _targets(self, read: ValuationRead, entry_mid: float | None) -> list[float]:
        """合理上限与前高：两只候选；未持仓时只留高于买入区中值的那几只。"""
        candidates = [value for value in (read.plan.fair.high, read.recent_high) if value]
        if entry_mid is not None:
            candidates = [value for value in candidates if value > entry_mid]
        return candidates

    # --- 屏幕上的那几句 -------------------------------------------------------------------

    def _price_evidence(self, quote: CardQuote) -> Evidence:
        origin = quote.detail or quote.source or "来处不明"
        day = quote.day if quote.day is not None else "没有数据日"
        return Evidence("价格", f"{quote.label or '—'}：{_fmt(quote.price)}；{origin}；{day}")

    def _evidence(
        self,
        shell: dict,
        read: ValuationRead,
        stop: float | None,
        stop_method: str,
        target: float | None,
        odds: OddsRange,
    ) -> tuple[Evidence, ...]:
        pr = read.fundamental.pr
        basis = read.fundamental.roe_basis
        value = (
            f"合理区间 {_fmt(read.plan.fair.low)}~{_fmt(read.plan.fair.high)}；"
            f"PR {_fmt(pr.value)}（口径 {basis}）；读数日 {read.as_of}"
        )
        risk = (
            f"止损 {_fmt(stop)}（{stop_method}）；目标 {_fmt(target)}；"
            + (odds.evidence or odds.reason or "R 留空")
            + f"；读数日 {read.as_of}"
        )
        return (
            self._price_evidence(shell["quote"]),
            Evidence("价值", value),
            Evidence("风险", risk),
        )

    def _vs_fair(
        self, price: float | None, fair_low: float | None, fair_high: float | None
    ) -> str:
        if price is None or fair_low is None or fair_high is None or fair_low <= 0:
            return "—"
        if price < fair_low:
            return f"低于合理下限 {(fair_low - price) / fair_low:.1%}"
        if price > fair_high:
            return f"高于合理上限 {(price - fair_high) / fair_high:.1%}"
        return "在合理区间内"

    def _distance(self, price: float | None, stop: float | None) -> float | None:
        if price is None or stop is None or price <= 0:
            return None
        return (price - stop) / price
