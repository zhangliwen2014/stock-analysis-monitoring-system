"""公司行为同步用例：能力路由取数 → 入库 → 写下覆盖确认（D36）。

与 `services.sync` 同构，多出来的只有第三步。前两步失败就整体失败，第三步成功才算
「这段区间我查过了」——覆盖标记是读取层守卫（`analysis/returns.py`）唯一的放行依据，
它说「截至这天，这只票的除权记录是齐的」。所以这里宁可少写一段覆盖面：
取数失败时什么都不记，让区间收益继续拒绝，而不是记下一条没根据的确认。

入库那一步同样会失败：两个源对同一天同一类型给出对不上的数时，主键装不下两条，
仓库直接抛 `StorageError`（见 `repos/corp_actions` 模块开头）。让它冒上来——
「这次同步没写完所以不确认」与「取数失败所以不确认」在读取层是同一种处境。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import DatasourceError
from sams.datasource.registry import AdapterRegistry, Attempt
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.corp_actions_coverage import CorpActionsCoverageRepo

logger = logging.getLogger("sams.services.corp_actions")


@dataclass(frozen=True)
class CorpActionSyncSummary:
    market: str
    code: str
    source: str = ""
    #: 取回的公司行为条数（0 条是合法答案：这只票这段区间确实没有除权）
    fetched: int = 0
    written: int = 0
    degraded: bool = False
    #: 链上因「没有公司行为能力」被跳过的源（D38）
    skipped: tuple[tuple[str, str], ...] = ()
    attempts: tuple[Attempt, ...] = ()
    #: 这次确认写到了哪里。全历史源的 `covered_to` 是今天而不是请求的末日
    covered_from: date | None = None
    covered_to: date | None = None
    full_history: bool = False
    #: 请求终点在今天之后时被截断到的那天；没截断就是 None
    clamped_to: date | None = None
    #: 这次一起说「查无记录」的源（0 条时才有值）。写覆盖确认时每个源各记一行，
    #: 屏幕上也要说出来：否则「akshare 一家说没有」看起来与「akshare 与同花顺都说没有」一样
    confirmed_empty_by: tuple[str, ...] = ()


class CorpActionSyncService:
    def __init__(
        self,
        registry: AdapterRegistry,
        actions: CorpActionRepo,
        coverage: CorpActionsCoverageRepo,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.registry = registry
        self.actions = actions
        self.coverage = coverage
        self.clock = clock

    def sync_actions(self, market: str, code: str, start: date, end: date) -> CorpActionSyncSummary:
        """幂等：同区间重复执行行数不变（(market, code, ex_date, kind) 主键覆盖写）。

        数值差一分以内算同一条记录的修正，照旧覆盖；差得多就是冲突，整批不写、
        确认也不写，等人来判该信哪个源。

        `end` 截到今天：今天之后的除权日没有任何源能替我们确认，取回来也不该写成
        「确认过的覆盖面」——那等于让未来的公告替过去的区间背书（PIT）。

        市场名与代码一起在入口归一：覆盖确认的主键含市场，写成 `a` 的那一行
        读取层永远查不到，于是「同步成功了」与「区间收益继续拒绝」同时成立。
        """
        market = canonical_market(market)
        code = canonical_code(market, code)
        today = self.clock.today()
        until = min(end, today)
        if start > until:
            raise DatasourceError(
                "corp_actions",
                f"{market} {code} 的请求区间 {start}~{end} 整体在今天（{today}）之后："
                "还没发生的日子无从确认",
            )
        clamped = until if until < end else None

        result = self.registry.fetch_actions(market, code, start, until)
        written = self.actions.upsert_many(result.frame)
        # 全历史源的答复是「截至今天，这只票的除权记录就这些」，与请求窗口多窄无关；
        # 按区间拉的源只能确认它被拉的那一段。
        # `covered_from` 记的是这次实际落库的最早除权日：一个从没分过红的票，
        # 「全历史」的起点就是请求窗口的起点，编一个 1990 出来是把猜测写成事实
        covered_to = today if result.full_history else until
        covered_from = start
        if result.full_history and not result.frame.is_empty():
            covered_from = min(result.frame["ex_date"].to_list())
        # 「查无记录」是每个源各自说的一句话，确认就按源各记一行（覆盖表主键含 src）：
        # 一句空答复只有在链上每个有能力的源都说了没有之后，才配写成「这只票没分过红」
        for src in result.confirmed_empty_by or (result.source,):
            self.coverage.record(
                market,
                code,
                covered_from=covered_from,
                covered_to=covered_to,
                full_history=result.full_history,
                src=src,
            )
        logger.info(
            "%s %s 公司行为 %d 条（源 %s，确认至 %s%s）",
            market,
            code,
            written,
            result.source,
            covered_to,
            "，全历史" if result.full_history else "",
        )
        return CorpActionSyncSummary(
            market=market,
            code=code,
            source=result.source,
            fetched=result.frame.height,
            written=written,
            degraded=result.degraded,
            skipped=result.skipped,
            attempts=result.attempts,
            covered_from=covered_from,
            covered_to=covered_to,
            full_history=result.full_history,
            clamped_to=clamped,
            confirmed_empty_by=result.confirmed_empty_by,
        )
