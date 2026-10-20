"""汇率的同步用例：把降级链的答复搬进 `fx_rates`，除此之外不决定任何事。

不放 `services/sync.py`：那个类按 `(market, code)` 组织，塞进来会让下一个读者以为
汇率也走市场路由。它也不解释数据——「今天的 USD→CNY 该用哪一天」是
`PortfolioService` 换算时问 `FxRepo.latest` 的事（D73 锚在行情日），不是这里。

一条红线：**链没答上不许落库**。半次成功写成一次成功，之后持仓那一屏的
`fx_pnl` 就建立在这次沉默上——它会显示一个看起来正常的 0。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from sams.core.errors import StorageError
from sams.datasource.registry import AdapterRegistry, Attempt
from sams.storage.repos.fx import FxRate, FxRepo

logger = logging.getLogger("sams.services.fx")


@dataclass(frozen=True)
class FxSyncSummary:
    """汇率同步的结果。

    不复用 `SyncSummary`：它的必填字段是 `market`/`code`，而外汇没有市场维度——
    把 `market=""` 塞进去，每一行打印都会多出两个空格和一句读不通的话。
    """

    base: str
    quote: str
    source: str = ""
    fetched: int = 0
    written: int = 0
    degraded: bool = False
    #: 每个有能力的源都独立答了「这段没有报价」：可以说「这几家都没有」，不能说「它没有」
    empty: bool = False
    #: 链上没有 `fx_rate` 的源（D38）：一个都没有时这一栏就是那句要说出口的缺
    skipped: tuple[tuple[str, str], ...] = ()
    attempts: tuple[Attempt, ...] = ()
    confirmed_empty_by: tuple[str, ...] = ()
    #: 最后写进去的那一天：`sams health` 用它判断汇率新不新
    latest_date: date | None = None


class FxSyncService:
    def __init__(self, registry: AdapterRegistry, fx: FxRepo) -> None:
        self.registry = registry
        self.fx = fx

    def sync(self, base: str, quote: str, start: date, end: date) -> FxSyncSummary:
        """汇率落进 `fx_rates`：跨币种持仓的 `fx_pnl` 全靠这张表有数。

        区间一次取（不在服务层按日循环）：源的历史接口本来就给一段，
        按日循环会把一次限流风险变成三十次。落库逐行走 `FxRepo.put`，
        它自带 `(date, base, quote)` 的幂等闸门，重跑不多出也不改动行。
        """
        result = self.registry.fetch_fx(base, quote, start, end)
        if result.empty_everywhere:
            # 一行都不写：写一行 0 或空记录，下次读取就会把「我们没查到」当成「它没有」
            return FxSyncSummary(
                base=base,
                quote=quote,
                source=result.source,
                empty=True,
                skipped=result.skipped,
                attempts=result.attempts,
                confirmed_empty_by=result.confirmed_empty_by,
            )
        rows = result.frame.to_dicts()
        for row in rows:
            self.fx.put(row["date"], base, quote, row["rate"], source=row["source"])
        logger.info("%s→%s 入库 %d 行（源 %s）", base, quote, len(rows), result.source)
        return FxSyncSummary(
            base=base,
            quote=quote,
            source=result.source,
            fetched=result.frame.height,
            written=len(rows),
            degraded=result.degraded,
            skipped=result.skipped,
            attempts=result.attempts,
            latest_date=result.frame["date"].max(),
        )

    def set_manual(self, base: str, quote: str, day: date, rate: float) -> FxRate:
        """采集链取不到时的出口：手工填一行，并把「这是手工的」写进库里那一列。

        走的是与 `sync` 同一个 `FxRepo.put`，不另开一条写路径：正数、有限、同币种那三道
        闸门在仓库里，抄一份到服务层迟早不一致。落库后再读一次返回——命令行走的是
        **库里的样子**，不是调用方传进来的那串字符。
        """
        self.fx.put(day, base, quote, rate, source="manual")
        stored = self.fx.get(day, base, quote)
        # 写完读不回来是库在说「这一行没落下去」：把它报成成功，下一层就拿着一个
        # 不存在的汇率去拆换汇影响
        if stored is None:  # pragma: no cover - put 之后必有一行
            raise StorageError(f"{day} 的 {base}→{quote} 汇率写完读不回来")
        logger.info("手工汇率 %s→%s %s = %g", base, quote, day, rate)
        return stored
