"""钱相关的纯数学（spec §5.4）。

放在 `core` 且不许 import 上层，是因为这段算术会被 T-8/T-9/T-12 和 M3-C 的规则引擎
反复用到：一旦有两份略微不同的换算，账本就从「可核对」变成「两份对不上的数」。
这里没有 IO：汇率、价格、均价都由调用方传进来，函数只负责算对。
"""

from __future__ import annotations

from dataclasses import dataclass

from sams.core.codes import canonical_code, canonical_market, currency_override
from sams.core.config import MarketCfg
from sams.core.errors import ValidationError

#: 超过这个天数的汇率算「陈旧」：4 = 周五汇率撑到周一不算陈旧，跨长假算。
FX_STALE_DAYS = 4

#: 报告币种里常见的三种符号；其余显示 `1234.50 XXX`，不猜符号。
_SYMBOLS = {"CNY": "¥", "USD": "$", "HKD": "HK$"}


def quote_currency(market: str, code: str, cfg: dict[str, MarketCfg]) -> str:
    """这个标的用什么币种记账（§5.4 硬规则 1：录入与存储永远用标的币种）。

    `currency_override` 先于市场默认值：B 股（`200*`/`900*`）以 USD/HKD 交易，
    HK 双柜台以 RMB 交易（D31）。反过来用市场币种会把同一只票的两个柜台
    记成两种成本。
    """
    key = canonical_market(market)
    try:
        currency = cfg[key].currency
    except KeyError:
        raise ValidationError(
            f"市场 {key} 未配置，给不出标的币种；"
            f"请在配置的 [markets.{key}] 里补 currency"
        ) from None
    return currency_override(key, canonical_code(key, code)) or currency


def blended_cost(old_qty: float, old_avg: float, qty: float, price: float, fee: float) -> float:
    """加仓后的加权均价；手续费计入成本（§5.4「含费」口径）。"""
    if old_qty <= 0:
        return (qty * price + fee) / qty
    return (old_qty * old_avg + qty * price + fee) / (old_qty + qty)


def realized_pnl(qty: float, price: float, avg_cost: float, fee: float) -> float:
    """卖出实现的盈亏，标的币种，已扣手续费。"""
    return qty * (price - avg_cost) - fee


def weighted_rate(entries: list[tuple[float, float, bool]]) -> float | None:
    """按金额加权的汇率；`entries` 是 (该笔金额的标的币种数额, 其汇率, is_stale)。

    先剔陈旧：一个过期汇率混进多币种汇总，等于让周五的汇率决定周一的总市值。
    全空或全是陈旧返回 `None` 而不是 0——0 会把市值抹平成 0，而「算不出来」和
    「值是 0」是两件事。取数归调用方（`core` 不查库），这里只做算术。
    """
    usable = [(amount, rate) for amount, rate, stale in entries if not stale and amount > 0]
    if not usable:
        return None
    total_amount = sum(amount for amount, _ in usable)
    if total_amount <= 0:
        return None
    return sum(amount * rate for amount, rate in usable) / total_amount


@dataclass(frozen=True)
class PositionPnl:
    """一个持仓的两段盈亏。字段单位写在注释里，因为混读它们是本模块唯一的危险。"""

    qty: float
    avg_cost: float
    last_price: float
    price_pnl: float  # 标的币种
    fx_pnl: float  # 报告币种
    market_report: float  # 报告币种：qty*last_price*rate_today
    cost_report: float  # 报告币种：qty*avg_cost*rate_base
    rate_base: float | None
    rate_today: float | None
    rate_stale: bool

    @property
    def total_report(self) -> float:
        """报告币种里的「市值 − 成本」。恒等式：`price_pnl*rate_base + fx_pnl` 与它相等。"""
        return self.market_report - self.cost_report


def position_pnl(
    *,
    qty: float,
    avg_cost: float,
    last_price: float,
    rate_base: float | None,
    rate_today: float | None,
    rate_today_stale: bool = False,
) -> PositionPnl:
    """把持仓盈亏拆成价格段与换汇段（§5.4 line 434）。

    `rate_base` 是**下单那天**的汇率（`sim_orders.fx_rate_ref` 加权），`rate_today`
    是今天。两者相同（同币种，或汇率没动）时 `fx_pnl` 为 0。传 `None` 表示同币种，
    此时不做换算，`market_report` 直接按 1:1 计入——这是报告币种=标的币种的正常
    路径，不是缺数降级。
    """
    price_pnl = qty * (last_price - avg_cost)
    if rate_base is None or rate_today is None:
        fx_pnl = 0.0
        market_report = qty * last_price
        cost_report = qty * avg_cost
        stale = False
    else:
        fx_pnl = qty * last_price * (rate_today - rate_base)
        market_report = qty * last_price * rate_today
        cost_report = qty * avg_cost * rate_base
        stale = rate_today_stale
    return PositionPnl(
        qty=qty,
        avg_cost=avg_cost,
        last_price=last_price,
        price_pnl=price_pnl,
        fx_pnl=fx_pnl,
        market_report=market_report,
        cost_report=cost_report,
        rate_base=rate_base,
        rate_today=rate_today,
        rate_stale=stale,
    )


def money_note(amount: float, currency: str) -> str:
    """带币种的金额文本（§5.4 硬规则 3：金额阈值必须带币种）。"""
    symbol = _SYMBOLS.get(currency)
    if symbol is None:
        return f"{amount:.2f} {currency}"
    return f"{symbol}{amount:.2f}"
