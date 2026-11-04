"""统一报价读模型（S0-E）：五页同源，只在这里决定「这一格是哪种价、哪一天、哪个源」。

盘中有过闸的报价就用报价；休市、停更或根本没有报价时回落 `bars_daily` 的最后一根
**不复权**日线，并把日期与来源一起交给页面。这样周六打开五个页面，同一只票给出的是
同一个数、同一句「10-30 收盘」，而不是看板停在周五 14:47、选股页却是收盘快照。

为什么不是「板上有价就一直用」：收盘后的 14:47 盘中价不是这一天收在哪，
持仓的盈亏、止损的判断与第二天的复盘都按收盘价对账。$D232$ 那条闸说得很清楚——
休市时的报价没有资格叫现价。

模型字段对应用户能问出口的每一件事：

* `price` / `pct`：值；
* `currency` / `unit`：币种（B 股、港股双柜台先认标的例外）；
* `day`：数据日（市场本地日，不是服务器日历日）；
* `tz`：市场时区（盘中时刻按它显示）；
* `kind`：`live` / `delayed` / `close` / `stale` / `none`；
* `adjustment`：`raw`（库内日线只有不复权一套）；
* `source`：腾讯、新浪或日线源；
* `label` / `detail`：屏幕上的「14:47 盘中」与「不复权 · akshare」。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sams.bootstrap import AppCore, ServiceContext
from sams.core.codes import canonical_code, canonical_market, currency_override
from sams.core.errors import SamsError
from sams.core.quotes import (
    DELAYED,
    LIVE,
    NO_DATA,
    Quote,
    local_day,
    quote_state,
    source_label,
)
from sams.storage.repos.bars import LastBar
from sams.web.display import currency_label, price_point_label

#: 库内日线只有不复权一套（`storage/repos/bars.ADJUST`）：回落那一格的价基恒是它
RAW_ADJUSTMENT = "raw"


@dataclass(frozen=True)
class QuoteView:
    price: float | None
    pct: float | None
    #: live / delayed / close / stale / none
    kind: str
    #: `quote_state` 的五态：badge 说市场此刻是什么，kind 说这个数是什么
    state: str
    day: date | None
    at: datetime | None
    source: str
    currency: str
    unit: str
    adjustment: str
    #: 「14:47 盘中」/「10-30 收盘」/「10-30 14:47 旧价」
    label: str
    #: 「腾讯」/「不复权 · akshare」：落在 label 旁边的那半句来处
    detail: str


def market_open(ctx: ServiceContext, market: str, now: datetime) -> bool:
    """日历读不到时按休市显示：五态里「休市」最不会催人动手。

    库被调度线程握着时这里会撞 `DatabaseLocked`，那是常态而不是事故：页面该继续
    给出它能给的东西，而不是一屏 503。
    """
    try:
        return ctx.calendar.is_market_open(market, now)
    except SamsError:
        return False


def market_timezone(ctx: ServiceContext, market: str) -> str:
    """报价时刻按**市场**时区显示：美股 16:00 收盘不该写成北京时间次日 04:00。

    数据日（`day`）与盘中时刻必须属于同一个时区，否则「10-30 收盘」与「04:00」
    并排出现时，读的人不知道这两个数说的是哪一天。
    """
    cfg = ctx.settings.markets.get(market)
    return cfg.tz if cfg else "UTC"


def currency_of(ctx: ServiceContext, market: str, code: str = "") -> str:
    cfg = ctx.settings.markets.get(market)
    return currency_override(market, code) or (cfg.currency if cfg else "")


def price_unit(ctx: ServiceContext, market: str, code: str = "") -> str:
    """这一行现价的单位：先认标的的币种例外（B 股、双柜台），再落市场的配置币种。"""
    return currency_label(currency_of(ctx, market, code))


class QuoteReader:
    """一次请求一份：市场开没开按市场问一次，回落日线按这批代码一次问完。"""

    def __init__(self, ctx: ServiceContext, core: AppCore, now: datetime) -> None:
        self._ctx = ctx
        self._core = core
        self._now = now
        self._open: dict[str, bool] = {}

    def _market_open(self, market: str) -> bool:
        if market not in self._open:
            self._open[market] = market_open(self._ctx, market, self._now)
        return self._open[market]

    def _state(self, quote: Quote | None, market: str) -> str:
        return quote_state(
            quote,
            now=self._now,
            market_open=self._market_open(market),
            stale_s=self._ctx.settings.quote_stale_s,
        )

    def view(self, market: str, code: str) -> QuoteView:
        market = canonical_market(market)
        code = canonical_code(market, code)
        return self.views(market, [code])[code]

    def views(self, market: str, codes: list[str]) -> dict[str, QuoteView]:
        """这一批代码此刻各自该显示什么。键按读侧同一条规则归一到市场形态。"""
        market = canonical_market(market)
        wanted = list(dict.fromkeys(canonical_code(market, code) for code in codes))
        quotes = {code: self._core.board.get(market, code) for code in wanted}
        states = {code: self._state(quotes[code], market) for code in wanted}
        fallback = [
            code for code in wanted if states[code] not in (LIVE, DELAYED)
        ]
        bars = (
            self._ctx.bars.last_bars([(market, code) for code in fallback])
            if fallback
            else {}
        )
        return {
            code: self._build(market, code, quotes[code], states[code], bars.get((market, code)))
            for code in wanted
        }

    def stream(self, market: str, code: str) -> QuoteView | None:
        """只看行情板：盘中价才有值，休市/陈旧交给调用方回落日线（持仓页要算盈亏）。

        与 `view` 分开是为了不在这里多读一次日线：持仓页原本就有一趟自己的收盘查询，
        再让这里替它读一遍，只是把同一根 K 线读两次再丢掉一次。
        """
        market = canonical_market(market)
        code = canonical_code(market, code)
        quote = self._core.board.get(market, code)
        state = self._state(quote, market)
        if state not in (LIVE, DELAYED) or quote is None:
            return None
        return self._live(market, code, quote, state)

    def _live(self, market: str, code: str, quote: Quote, state: str) -> QuoteView:
        tz = market_timezone(self._ctx, market)
        day = local_day(quote.ts, ZoneInfo(tz))
        currency = currency_of(self._ctx, market, code)
        return QuoteView(
            price=quote.price,
            pct=quote.pct_chg,
            kind=state,
            state=state,
            day=day,
            at=quote.ts,
            source=quote.source,
            currency=currency,
            unit=currency_label(currency),
            adjustment=RAW_ADJUSTMENT,
            label=price_point_label(state, day, quote.ts, tz),
            detail=source_label(quote.source),
        )

    def _build(
        self,
        market: str,
        code: str,
        quote: Quote | None,
        state: str,
        bar: LastBar | None,
    ) -> QuoteView:
        if state in (LIVE, DELAYED) and quote is not None:
            return self._live(market, code, quote, state)
        tz = market_timezone(self._ctx, market)
        currency = currency_of(self._ctx, market, code)
        unit = currency_label(currency)
        if bar is not None:
            pct = None
            if bar.prev_close is not None and bar.prev_close > 0:
                pct = (bar.close / bar.prev_close - 1) * 100
            return QuoteView(
                price=bar.close,
                pct=pct,
                kind="close",
                state=state,
                day=bar.day,
                at=None,
                source=bar.source,
                currency=currency,
                unit=unit,
                adjustment=RAW_ADJUSTMENT,
                label=price_point_label("close", bar.day),
                detail=f"不复权 · {source_label(bar.source)}",
            )
        if quote is not None:
            day = local_day(quote.ts, ZoneInfo(tz))
            return QuoteView(
                price=quote.price,
                pct=quote.pct_chg,
                kind="stale",
                state=state,
                day=day,
                at=quote.ts,
                source=quote.source,
                currency=currency,
                unit=unit,
                adjustment=RAW_ADJUSTMENT,
                label=price_point_label("stale", day, quote.ts, tz),
                detail=source_label(quote.source),
            )
        return QuoteView(
            price=None,
            pct=None,
            kind="none",
            state=NO_DATA if state in (LIVE, DELAYED) else state,
            day=None,
            at=None,
            source="",
            currency=currency,
            unit=unit,
            adjustment="",
            label="—",
            detail="",
        )


def quote_reader(ctx: ServiceContext, core: AppCore, now: datetime) -> QuoteReader:
    return QuoteReader(ctx, core, now)
