"""报价的域值对象与「这一笔现在算哪种状态」。

`ts` 是**源给的报价时刻**，不是取回时刻：交易所的时钟与这台机器的时钟不是一回事，
用取回时刻算 age 会把一次延迟 5 分钟的源显示成实时。源不给时刻时由适配器用取回时刻
兜底并置 `is_stale=True`——那一句的意思是「这个时间不是源说的」，不是「它旧」。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sams.core.errors import ValidationError

#: 交易所所在地的时区。这是市场属性，不是用户展示偏好（后者是 `server.display_tz`）。
MARKET_ZONES: dict[str, ZoneInfo] = {
    "A": ZoneInfo("Asia/Shanghai"),
    "HK": ZoneInfo("Asia/Hong_Kong"),
    "US": ZoneInfo("America/New_York"),
}

LIVE, DELAYED, CLOSED, STALE, NO_DATA = "live", "delayed", "closed", "stale", "no_data"


@dataclass(frozen=True)
class Quote:
    market: str
    code: str
    #: 一律 UTC：展示时才按市场时区折算
    ts: datetime
    price: float
    open: float | None = None
    prev_close: float | None = None
    high: float | None = None
    low: float | None = None
    pct_chg: float | None = None
    volume: float | None = None
    amount: float | None = None
    bid1: float | None = None
    ask1: float | None = None
    #: 源侧交易状态（有就带，不猜）：停牌、盘前之类只有源知道
    status: str = ""
    #: 源明确标了延迟才为 True；`None` 是「没标」，不等于「保证实时」
    delayed: bool | None = None
    source: str = ""
    #: 源没给时刻（或给了读不懂的格式），于是 `ts` 是取回时刻
    is_stale: bool = False

    def __post_init__(self) -> None:
        if not self.market or not self.code:
            raise ValidationError("Quote 的 market/code 不能为空")
        if not math.isfinite(self.price) or self.price <= 0:
            raise ValidationError(f"报价价格必须有限且为正: {self.price!r}")
        if self.ts.tzinfo is None:
            raise ValidationError("Quote.ts 必须是带时区的 UTC 时刻")

    def marked_stale(self) -> Quote:
        return replace(self, is_stale=True)


@dataclass(frozen=True)
class QuoteBatch:
    """一个源对一批代码的一次答复。

    `skipped` 与 `errors` 分开：前者是「源明确没有这只票」（不重试），后者是
    「响应里有这只票但读不懂」（值班时要看的那一句）。两者都**不**写 0。
    """

    quotes: tuple[Quote, ...] = ()
    skipped: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class QuoteSourceRecord:
    """报价源健康：与日线的 `SourceRecord` 分开一张表、分开一个类型。

    `last_ok_at` 是时刻而不是日期：盘中每 10 秒都在问，按天记的话「今天成功过一次」
    与「三小时前成功过一次」长得一样。
    """

    name: str
    markets: tuple[str, ...] = ()
    ok: bool | None = None
    last_error: str | None = None
    last_ok_at: datetime | None = None
    consecutive_failures: int = 0


def as_utc(value: datetime, zone: ZoneInfo) -> datetime:
    """源侧本地时刻 → UTC；已是 aware 的就只换算。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=zone)
    return value.astimezone(UTC)


def quote_state(
    quote: Quote | None,
    *,
    now: datetime,
    market_open: bool,
    stale_s: int = 60,
) -> str:
    """五态（D134）。顺序就是修法的顺序：先看市场开没开，再看有没有、旧不旧。

    `closed` 优先于 `stale`：周末看到一条周五的报价不是故障，页面不该把它染红。
    `delayed` 排在 `stale` 之后：源自己说延迟 15 分钟、而这一笔已经 20 分钟没动，
    前者是常态、后者才是要查的事。
    """
    if not market_open:
        return CLOSED
    if quote is None:
        return NO_DATA
    age = max(0.0, (now - quote.ts).total_seconds())
    if quote.is_stale or age > stale_s:
        return STALE
    if quote.delayed is True:
        return DELAYED
    return LIVE
