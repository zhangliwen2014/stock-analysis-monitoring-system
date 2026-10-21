"""规则引擎唯一的取数处（spec §10「输入」那一列）。

拆成两个动作：`expand` 回答「这条规则覆盖哪些对象」，`read` 回答「那个对象此刻是什么数」。
分开的理由是冷却：去重键里有 `account_id`，所以展开必须带着账户出门。

这一层是全套规则里**唯一**允许碰 IO 的地方。模板因此是纯函数（T-7 起），除权日那种错
在手搓的 `Snapshot` 上就能复现，不必等网络。

不认识的 target_type 一律抛，不返回空列表：空列表在屏幕上长成「这条规则没触发」，
而它真实的意思是「这条规则谁也没看」。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

import polars as pl

from sams.analysis.indicators import metrics
from sams.core.codes import canonical_code, canonical_market
from sams.core.config import Settings
from sams.core.errors import ValidationError
from sams.services._fold import before_last, fold, fold_to_price_basis, high_water, last
from sams.services.analysis import AnalysisService, SeriesRead
from sams.services.valuation import ValuationService
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.ledger import Account, Position, SimAccountRepo, SimOrderRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.theses import Thesis, ThesisRepo
from sams.storage.repos.watchlists import WatchlistRepo

if TYPE_CHECKING:
    from sams.analysis.adjust import DayFactor
    from sams.services.rules.anchor import Moved

#: 日线一次取多少天：盖住 MA60（60 个交易日）与一段正常持有期。
#: 与 `valuation` 的窗口无关，所以它自己算一次：那边只带它合成结论要的那几路出门。
DAILY_LOOKBACK_DAYS = 400

#: 持仓不足这么多根日 K 时，「持仓期最高价」没有意义（第一根就是最高价，回撤恒为 0）。
TRAILING_MIN_BARS = 3

#: MA60 要哪一列：`indicators=("ma",)` 产出 ma5/ma10/ma20/ma60/ma120/ma250
_MA_COLUMN = "ma60"

#: 这一路要算的指标组。ATR 不在这里：它给自己留了一条别人没有的失败闸（最高/最低价
#: 缺数），与均线同批算的话，一根脏 K 会让整趟读取对这条规则全线不响。单独一路见 `_atr`。
_DAILY_INDICATORS = ("ma",)


@dataclass(frozen=True)
class Target:
    """一趟求值里的一个对象。`user_id` 也在里面：展开出来的一切都必须说清是谁的。"""

    user_id: str
    market: str
    code: str
    account_id: str | None = None
    thesis_id: str | None = None


@dataclass(frozen=True)
class Snapshot:
    """一个对象在一次求值里的全部事实，**现价口径 + 标的币种**。

    每个数与它的 `*_on` 同进同出：一条「跌破止损」必须说得出这个价是哪天的。
    所有 `None` 都是「没有这个数」而不是 0——0 在阈值语言里是一个能触发止损的值。
    """

    market: str
    code: str
    #: 显示名（`symbols` 里那一列；读不到就用代码本身，屏幕上不能是空的）
    name: str = ""
    currency: str = ""
    report_currency: str = ""
    account_id: str | None = None
    account_name: str = ""
    thesis_id: str | None = None
    #: 现价（过闸日线的裸收盘）与它属于哪天
    price: float | None = None
    price_on: date | None = None
    price_age_days: int | None = None
    #: 当日开盘与前收盘：`urgent`（跳空低开破位）与 `price.cross_*` 靠这两个数。
    #: 库里只有日线，所以开盘价来自 `BarRepo`，不是快照流（§3 的裁剪）
    day_open: float | None = None
    prev_close: float | None = None
    #: 持仓（None ⇒ 这个目标此刻没有持仓，持仓类模板一律不判）
    qty: float | None = None
    avg_cost: float | None = None
    opened_on: date | None = None
    holding_days: int | None = None
    #: 持仓期最高价，折回现价口径。不足 `TRAILING_MIN_BARS` 根 ⇒ None
    holding_high: float | None = None
    #: 折回现价口径的技术位
    weekly_ma20: float | None = None
    daily_ma60: float | None = None
    atr: float | None = None
    #: 论上那几列 + 本次合成的结论
    stop_line: float | None = None
    target_price: float | None = None
    odds: float | None = None
    score: float | None = None
    items: tuple[tuple[str, float], ...] = ()
    #: 最近一期财报的披露日（`scheduled.reevaluate` 的输入）
    disclosed_on: date | None = None
    #: 保证金账户：`notes` 据此说「爆仓线可能先到」
    margin_enabled: bool = False
    #: 这一路缺了什么（进告警正文那几行，`engine.py` 把它们逐条列在结论后面）
    missing: tuple[str, ...] = ()
    #: 本次读到的那段 K 线里记录在案的除权日。**空是常态**：库里那段没有除权时，
    #: 两个价空间是同一个，`anchor.py` 因此一个数都不动（它的口径 2）
    factors: tuple[DayFactor, ...] = ()
    #: 那段 K 线最早到哪天。锚比它早 ⇒ 中间那段的除权没记进来，搬它会少搬（`anchor.py` 的口径 1）
    bars_from: date | None = None
    #: 下面两列说的是**那一个数出自哪天**，取数处按来源给：抄自论上那一列就填那次读数日
    #: （0013 的 `levels_on`，手填的为空），本次合成的就填本趟那一天——
    #: 后者跨不过任何除权日，所以天然不搬（§10.2）
    #: 论上那条止损线属于哪天
    stop_on: date | None = None
    #: 论上那个目标价属于哪天
    target_on: date | None = None
    #: 这一趟搬了哪几个数（每个都带它跨了哪些除权日，见 `anchor.Moved`）
    moved: tuple[Moved, ...] = ()


class RuleData:
    """把八个仓库/服务包成两个动作。构造参数全是 `Container` 上已有的字段。"""

    def __init__(
        self,
        *,
        valuation: ValuationService,
        analysis: AnalysisService,
        bars: BarRepo,
        symbols: SymbolRepo,
        accounts: SimAccountRepo,
        ledger: SimOrderRepo,
        theses: ThesisRepo,
        watchlists: WatchlistRepo,
        settings: Settings,
    ) -> None:
        self.valuation = valuation
        self.analysis = analysis
        self.bars = bars
        self.symbols = symbols
        self.accounts = accounts
        self.ledger = ledger
        self.theses = theses
        self.watchlists = watchlists
        self.settings = settings

    # --- 展开 ---------------------------------------------------------------------------

    def expand(self, rule) -> list[Target]:
        """一条规则 → 一组对象。`enabled` 与冷却由调用方管，这里只管形状。"""
        kind = rule.target_type
        if kind == "symbol":
            market, _, code = rule.target_ref.partition("/")
            normalized = canonical_market(market)
            return [
                Target(
                    rule.user_id,
                    normalized,
                    canonical_code(normalized, code),
                    rule.account_id,
                    rule.thesis_id,
                )
            ]
        if kind == "watchlist":
            return [
                Target(rule.user_id, market, code, rule.account_id, rule.thesis_id)
                for market, code in self.watchlists.codes(rule.user_id)
            ]
        if kind == "position":
            if rule.account_id is None:
                raise ValidationError(
                    f"规则 {rule.name} 是持仓类却没有账户：去重键含 account_id，"
                    "缺了它同一只票在两个账户里的破位会互相吞（§10.1）"
                )
            return [
                Target(rule.user_id, row.market, row.code, rule.account_id, rule.thesis_id)
                for row in self.holdings(rule.account_id)
            ]
        if kind == "thesis":
            thesis = self._thesis_of_rule(rule)
            if thesis is None:
                return []
            return [Target(rule.user_id, thesis.market, thesis.code, rule.account_id, thesis.id)]
        raise ValidationError(
            f"target_type={kind} 这一层没有展开方式：目前只认 "
            "symbol/watchlist/position/thesis；组合级规则要的是每日组合快照（U28），"
            "而库里还没有那张表"
        )

    def holdings(self, account_id: str) -> list[Position]:
        """当前持仓（不含已清仓的 0 股行）。一趟里同一个账户只读一次由引擎保证：
        它先调这里拿表，再把每一行交给 `read(..., position=...)`。"""
        return [row for row in self.ledger.positions(account_id) if row.qty > 0]

    def thesis_for(self, target: Target) -> Thesis | None:
        if target.thesis_id is None:
            return None
        return self.theses.get(target.user_id, target.thesis_id)

    def _thesis_of_rule(self, rule) -> Thesis | None:
        return self.theses.get(rule.user_id, rule.thesis_id or rule.target_ref)

    # --- 读取 ---------------------------------------------------------------------------

    def read(
        self,
        target: Target,
        *,
        as_of: date,
        position: Position | None = None,
    ) -> Snapshot:
        """装配一个对象此刻的全部数。三路读取各有各的理由（见模块开头）。

        `position` 是引擎从 `holdings()` 里带来的那一行：带着它就不必读第二次，
        而两次读之间规则可以被改，那样算出来的成本与屏幕上那一条不是同一个数。
        """
        thesis = self.thesis_for(target)
        valuation = self.valuation.read(
            target.market,
            target.code,
            as_of=as_of,
            style=thesis.style if thesis is not None else "value",
        )
        daily = self.analysis.series(
            target.market,
            target.code,
            start=as_of - timedelta(days=DAILY_LOOKBACK_DAYS),
            end=as_of,
            indicators=_DAILY_INDICATORS,
        )
        account = self._account(target)
        missing: list[str] = []
        if valuation.price is None:
            missing.append("没有过闸的现价：这一趟不判任何价格类条件")
        atr = _atr(daily, valuation.price, missing)
        return Snapshot(
            market=target.market,
            code=target.code,
            name=self._name(target),
            currency=self._currency(target, position),
            report_currency=(
                account.report_currency
                if account is not None
                else self.settings.report_currency
            ),
            account_id=target.account_id,
            account_name=account.name if account is not None else "",
            thesis_id=target.thesis_id,
            price=valuation.price,
            price_on=valuation.price_on,
            price_age_days=valuation.price_age_days,
            day_open=self._day_open(target, as_of),
            prev_close=self._prev_close(daily.table, valuation.price),
            qty=None if position is None else position.qty,
            avg_cost=None if position is None else position.avg_cost,
            opened_on=None if position is None else _date_of(position.open_ts),
            holding_days=_holding_days(position, as_of),
            holding_high=_holding_high(daily.series.frame, valuation.price, position),
            weekly_ma20=valuation.weekly_ma20,
            daily_ma60=_fold_line(daily.table, _MA_COLUMN, valuation.price),
            atr=atr,
            stop_line=_stop_line(thesis, valuation),
            target_price=_target_price(thesis, valuation),
            odds=valuation.plan.odds.value,
            score=valuation.composite.score,
            items=tuple(
                sorted(
                    (item.key, item.value)
                    for item in valuation.composite.items
                    if item.value is not None
                )
            ),
            disclosed_on=valuation.fundamental.published_on,
            margin_enabled=bool(account is not None and account.margin_enabled),
            missing=tuple(missing),
        )

    def _name(self, target: Target) -> str:
        got = self.symbols.get(target.market, target.code) or {}
        return str(got.get("name") or target.code)

    def _account(self, target: Target) -> Account | None:
        if target.account_id is None:
            return None
        return self.accounts.get(target.user_id, target.account_id)

    def _currency(self, target: Target, position: Position | None) -> str:
        """标的币种：有持仓就用账本那一列，没有才退回市场配置。

        顺序反过来的话，一个把 `symbols.currency` 写歪的旧行会让阈值带上错误的币种后缀
        （§5.4 规则 3 禁止未标注币种的金额，而**标错**比不标更难查）。
        """
        if position is not None:
            return position.currency
        cfg = self.settings.markets.get(target.market)
        return cfg.currency if cfg is not None else ""

    def _day_open(self, target: Target, as_of: date) -> float | None:
        """当日开盘：库里只有不复权日线，裸价**就是**现价口径（不折）。"""
        frame = self.bars.load(target.market, [target.code], start=as_of, end=as_of)
        if frame.height == 0:
            return None
        value = frame["open"][0]
        return None if value is None else float(value)

    def _prev_close(self, table: pl.DataFrame, price: float | None) -> float | None:
        """前收盘折回现价口径：跨除权日时裸价那个「昨天」不是今天可比的那个「昨天」。"""
        if "close_hfq" not in table.columns:
            return None
        before = before_last(table, "close_hfq")
        if before is None:
            return None
        return fold_to_price_basis(float(before), fold(price, last(table, "close_hfq")))


# --- 纯算式（没有 IO，所以模块级）-------------------------------------------------------------


def _fold_line(table: pl.DataFrame, column: str, price: float | None) -> float | None:
    """把后复权口径的一条指标折到现价口径：因子来自**这条**日线序列的当日两个收盘。"""
    return fold_to_price_basis(last(table, column), fold(price, last(table, "close_hfq")))


def _atr(read: SeriesRead, price: float | None, missing: list[str]) -> float | None:
    """ATR 单独一路算：它有一条别人没有的失败闸（最高/最低价缺数）。

    那一根脏 K 已经被价格类的判断排除在外了；让 ATR 也退成 None 是诚实的，
    拉着同一条规则其他判据一起不响不是。
    """
    try:
        frame = metrics(read.series, ("atr",))
    except ValidationError:
        missing.append(
            "ATR 缺数：这段日线里有不过闸的最高/最低价，跳过它继续递推会让整条 ATR 与正解不同"
        )
        return None
    return fold_to_price_basis(
        last(frame, "atr"), fold(price, last(read.series.frame, "close_hfq"))
    )


def _holding_high(
    table: pl.DataFrame, price: float | None, position: Position | None
) -> float | None:
    """持仓期最高价（现价口径）。没有持仓、没有建仓时刻、或不足三根 ⇒ None（见常量注释）。"""
    if position is None or position.open_ts is None or table.height == 0:
        return None
    window = table.filter(pl.col("date") >= position.open_ts.date())
    if window.height < TRAILING_MIN_BARS:
        return None
    factor = fold(price, last(table, "close_hfq"))
    return fold_to_price_basis(high_water(window, window.height), factor)


def _stop_line(thesis: Thesis | None, valuation) -> float | None:
    """止损线优先用**论上那一列**（用户确认过的数），没有才退回本次合成的那一条。

    顺序反过来的话，用户手工把线抬到成本（`stop.breakeven` 之后常见）会被下一次自动
    合成覆盖——屏幕上那条线就不是他填的那一条了。
    """
    if thesis is not None and thesis.stop_price is not None:
        return thesis.stop_price
    return valuation.plan.stop.low if valuation.plan.stop.ok else None


def _target_price(thesis: Thesis | None, valuation) -> float | None:
    """目标价与止损线同一条优先级：论上那一列是用户确认过的数，没有才退回本次合成。"""
    if thesis is not None and thesis.target_price is not None:
        return thesis.target_price
    return valuation.plan.target.low if valuation.plan.target.ok else None


def _date_of(moment) -> date | None:
    return None if moment is None else moment.date()


def _holding_days(position: Position | None, as_of: date) -> int | None:
    if position is None or position.open_ts is None:
        return None
    return max(0, (as_of - position.open_ts.date()).days)
