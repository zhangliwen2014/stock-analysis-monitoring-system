"""把进程级报价板折成持仓页要的「盘中价或没有」（D232）。

闸与看板同一套 `quote_state`（`web/quotes.py` 的 `QuoteReader.stream`）：休市、陈旧、
没有报价都回落 `bars_daily` 的最后收盘；只有 `LIVE`/`DELAYED` 才顶掉收盘价，并在行上
标明这一行是哪种价、哪个时刻。

`day` 取报价所属**市场**的本地日期，不用服务器的展示时区：美股盘中的 UTC 日期
常常比纽约早一天，换算锚错一天就会去查一条还不存在的汇率。
"""

from __future__ import annotations

from datetime import datetime

from sams.bootstrap import AppCore, ServiceContext
from sams.services.portfolio import LivePrice
from sams.web.quotes import quote_reader


class BoardPrices:
    """一次请求一份：开市与否按市场缓存，逐行只做一次板上查找。"""

    def __init__(
        self, ctx: ServiceContext, core: AppCore, now: datetime
    ) -> None:
        self._ctx = ctx
        self._reader = quote_reader(ctx, core, now)

    def get(self, market: str, code: str) -> LivePrice | None:
        if market not in self._ctx.settings.markets:
            return None
        view = self._reader.stream(market, code)
        if view is None or view.price is None or view.day is None:
            return None
        return LivePrice(
            view.price,
            view.day,
            delayed=view.kind == "delayed",
            at=view.at,
        )


def board_prices(ctx: ServiceContext, core: AppCore, now: datetime) -> BoardPrices:
    return BoardPrices(ctx, core, now)
