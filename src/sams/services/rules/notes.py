"""执行约束文案（spec §10.2「必须写进告警文案」那一段）。纯函数，只产字符串。

为什么这一层单独存在：约束绑的是**标的所属市场**，不是账户（D21）——同一个账户里的
A 股仍 T+1、美股仍 T+0。把这几句话拼在投递那里，就会有两个通道各自拼一遍，
而 §10.2 要的正是「每条告警都带、措辞一致」。

不查库：市场事实从 `Snapshot.market` 与 `core.codes` 现推，汇率由引擎（T-14）当作
参数递进来（`Fx`）。这一段文案因此在没有库的测试里也跑得出——取向同 `filters`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sams.core.codes import canonical_market, daily_price_limit
from sams.core.errors import ValidationError
from sams.services.rules.snapshot import Snapshot

#: 每个市场的交割规则。这一张表在这里是唯一的一份：`CalendarService` 管时段，
#: `daily_price_limit` 管涨跌幅，都不管交割，所以 T+1 这个词只能出现在这个文件里。
#: 加一种市场就加一行——没有配置项是故意的：交割规则是市场事实，不是用户偏好。
_SETTLEMENT = {"A": "T+1", "HK": "T+0", "US": "T+0"}

#: 跌停判据的余量。交易所把涨跌停价**四舍五入到分**，所以只容忍「差一分」，不容忍
#: 「差一个百分比」：按百分比留余量（哪怕只有 1.5%），10% 板上跌到 8.65% 那天会被说成
#: 「跌停封板」——那是一句关于可执行性的假话（D96）。一分恰好盖住「交易所进位、我们
#: 用 round 舍位」这类舍入分歧（见 `test_the_limit_down_test_has_room_for_rounding`）。
#: 方向仍选**放宽**（更容易判成封板）：多判一句的代价只是文案长一点，漏判的代价是
#: 用户按一个挂不出去的价去操作。
_LIMIT_DOWN_TICK = 0.01


@dataclass(frozen=True)
class Fx:
    """汇总口径那一行汇率。方向写死：**1 单位标的币种 = rate 单位汇总币种**。

    来源固定为引擎（T-14）那次读数：`FxRepo.latest(day, 标的币种, 汇总币种)`——base 是
    `snapshot.currency`、quote 是 `snapshot.report_currency`。读的时候**不取倒数**：
    方向写反的症状是「大约对」，那是最难查的错（同一约定见汇率读写那一处）。
    `currency` 必须等于 `snapshot.report_currency`，否则它折合的是另一种币，
    `holding_pnl` 宁可说「没读到」也不印一个不是汇总口径的数。
    """

    currency: str
    #: 1 单位标的币种 = rate 单位 `currency`（汇总币种）。不许在这里取倒数。
    rate: float
    on: date
    #: `FxRepo` 那个数过了多少天：过期汇率要说得出自己过期（§5.4 规则 3 的另一半）
    age_days: int = 0


def _limit_down(snapshot: Snapshot, market: str) -> tuple[bool, float | None]:
    """现价是否贴在跌停板上。`market` 传**归一后**的值（"" ⇒ 认不出，判不了）。

    返回 (是否, 那一板的幅度)——幅度给文案用，不写死 10%。归一做一次传下去：不这样
    的话 `daily_price_limit` 内部会拿原始 `snapshot.market` 再归一一遍，空市场名在那里
    抛 `ValidationError`，把整趟求值打掉。
    """
    band = daily_price_limit(market, snapshot.code) if market else None
    if band is None or snapshot.prev_close is None or snapshot.price is None:
        return False, band
    floor = round(snapshot.prev_close * (1.0 - band), 2)
    return snapshot.price <= floor + _LIMIT_DOWN_TICK + 1e-9, band


def constraint_lines(snapshot: Snapshot) -> tuple[str, ...]:
    """这一笔今天能不能按告警里的价执行，一次说完。没有一条约束时返回空元组。

    顺序固定（交割 → 涨跌幅 → 保证金）：读的人要先知道「今天卖不卖得掉」，
    再知道「卖得掉的话有没有额外的线在前面」。
    """
    lines: list[str] = []
    try:
        market = canonical_market(snapshot.market)
    except ValidationError:
        # 认不出市场 ⇒ 交割与涨跌幅两句话都不说。取向同 `filters.no_open_alert`：
        # 这段文案引擎每趟都调，一行脏 market 不该把整趟告警崩掉；判不出就放行，
        # 而 band 那侧本来就靠 `daily_price_limit` 给 None 自然沉默。
        market = ""
    if market:
        rule = _SETTLEMENT.get(market)
        if (
            rule == "T+1"
            and snapshot.opened_on is not None
            and snapshot.price_on is not None
            and snapshot.opened_on == snapshot.price_on
        ):
            # 「今日买入」的证据其实是「建仓日 == 现价日」，所以那句话里带上这一天：
            # 现价陈旧时「今日」会不准，而日期是 `Snapshot` 里查得出的事实。
            lines.append(
                f"{snapshot.price_on} 建仓、现价同为该日读数：受 T+1 限制当日无法卖出，"
                "这一条要等下一个交易日执行"
            )
    down, band = _limit_down(snapshot, market)
    if down and band is not None:
        lines.append(
            f"跌停封板（{band:.0%}）附近，可能无法按告警里的价成交："
            "挂单排队与撤单时效由券商决定，这一条不是「按价必得」"
        )
    if snapshot.margin_enabled:
        lines.append(
            "该账户开启保证金：维持担保比例可能先于本条触发券商处置（该规则本身在 M5）"
        )
    return tuple(lines)


def holding_pnl(snapshot: Snapshot, *, fx: Fx | None = None) -> str:
    """浮动盈亏那一行，两个币种各带自己的标注（§5.4 规则 3；§10.2 最后一条）。

    这里是**标的币种**的浮盈亏，按持仓均价与现价算，不含费用（费用由 `sams ledger` 说）。
    跨币种而没读到汇率时明写「没读到」而不省略：省略的话那一屏上只有标的币种，
    用户会把它当成汇总口径去加总。
    """
    if snapshot.qty is None or snapshot.avg_cost is None or snapshot.price is None:
        return ""
    amount = (snapshot.price - snapshot.avg_cost) * snapshot.qty
    word = "浮盈" if amount >= 0 else "浮亏"
    # 方向交给 `word`（浮盈/浮亏）说：这里再带一个负号，就与下一行不带符号的折合数
    # 在同一句话里一个有号一个没号。
    base = f"{word} {abs(amount):,.2f} {snapshot.currency or '元'}"
    report = snapshot.report_currency
    if not report or report == snapshot.currency:
        return base
    if fx is None or fx.currency != report:
        # 折错了币（fx 的 quote 不是汇总币种）与没读到是同一种「给不出汇总数」：
        # 与其印一个不是汇总口径的折合数，不如说这一趟没读到 {标的}→{汇总} 那一条。
        return (
            f"{base}（汇总币种 {report}：这一趟没读到 {snapshot.currency}→{report} "
            "的汇率，不给折合数）"
        )
    stale = "" if fx.age_days == 0 else f"，{fx.age_days} 天前"
    return (
        f"{base}，折 {fx.currency} {abs(amount) * fx.rate:,.2f}"
        f"（{fx.on} 汇率 {fx.rate:g}{stale}）"
    )
