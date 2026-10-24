"""给模板用的显示函数：数字怎么排、状态叫什么，只在这里定一次。

`pct_chg` 本来就是百分数（0.65 = 0.65%），这里不要再乘 100。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sams.core.quotes import CLOSED, DELAYED, LIVE, NO_DATA, STALE

_STATE_LABEL = {
    LIVE: "实时",
    DELAYED: "延迟",
    CLOSED: "休市",
    STALE: "停更",
    NO_DATA: "无行情",
}


def state_label(state: str) -> str:
    return _STATE_LABEL.get(state, state)


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}%"


def pct_class(value: float | None) -> str:
    if value is None:
        return ""
    return "up" if value > 0 else ("down" if value < 0 else "")


def money(value: float | None, currency: str = "") -> str:
    if value is None:
        return "—"
    tail = f" {currency}" if currency else ""
    return f"{value:,.2f}{tail}"


def hms(moment: datetime | None, tz: str | None = None) -> str:
    """时刻显示到秒；给了 `tz` 就折算到那个时区再取时分秒。

    板上的时刻一律 UTC（`Quote.ts` 与 `MarketBoard.last_poll_at` 都是）：不折算就
    会把 15:00 的收盘写成 07:00，看上去像行情停更了八个小时。库里读出来的 naive
    值不猜它属于哪个时区，按原样显示——猜错比不猜更难查。
    """
    if moment is None:
        return "—"
    if tz and moment.tzinfo is not None:
        moment = moment.astimezone(ZoneInfo(tz))
    return moment.strftime("%H:%M:%S")
