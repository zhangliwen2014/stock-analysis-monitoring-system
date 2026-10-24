"""每一轮报价之后的那一趟求值（spec §5.3）：它自己不碰网络，也不碰库以外的东西。

输入是 `IntradayMonitor.poll_once` 的产物，输出是一张小结。逐用户 × 逐活市场调
`RuleEngine.check(intraday=True)`：求值链、过滤、冷却、投递全在引擎与 router 那两头，
这里只负责「叫谁、叫几次、坏了怎么办」。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sams.core.errors import SamsError
from sams.core.rules import RULE_KINDS, is_intraday
from sams.services.intraday import MarketPoll
from sams.services.quotes import QuoteBoard
from sams.storage.repos.rules import RuleRepo

logger = logging.getLogger("sams.services.rules.intraday")


@dataclass(frozen=True)
class IntradayRound:
    """一轮的账。`rules` 是这一趟**看过**几条（不是触发几条），`hits` 才是触发。"""

    markets: tuple[str, ...] = ()
    rules: int = 0
    hits: int = 0


class IntradayEvaluator:
    def __init__(self, rules: RuleRepo, engine, board: QuoteBoard) -> None:
        self.rules = rules
        self.engine = engine
        self.board = board

    def after_poll(self, outcomes: tuple[MarketPoll, ...]) -> IntradayRound:
        """一轮报价之后的求值。有报价的市场才叫「活」：skip 与 fail 的市场不动。"""
        live = sorted(
            {
                poll.market
                for poll in outcomes
                if poll.result is not None and poll.result.quotes
            }
        )
        if not live:
            return IntradayRound()
        try:
            # 名单只按**认识的** kind 的模板旗标滤：`is_intraday` 对名单外的 kind 是抛
            # KeyError（`TEMPLATES` 是唯一出处），而 KeyError 不是 `SamsError`，逃得过下面
            # 那道 except——拿它当筛子就等于让库里躺着的一条野行把轮询线程拖崩。野行留在
            # 名单里交给引擎：那里每个市场各落一句 skipped，别的用户与市场照跑（与 Task 8
            # 修过的 `_select` 同一个取向）。
            enabled = [
                rule
                for rule in self.rules.list_enabled()
                if rule.kind not in RULE_KINDS or is_intraday(rule.kind)
            ]
        except SamsError:
            # 读不到启用规则等于这一趟全盲：记下来，别让轮询线程跟着死
            logger.exception("盘中规则评估这一轮失败：读不到启用的规则")
            return IntradayRound(markets=tuple(live))
        users = sorted({rule.user_id for rule in enabled})
        hits = 0
        for user_id in users:
            for market in live:
                try:
                    report = self.engine.check(
                        user_id, market=market, intraday=True, quotes=self.board
                    )
                except SamsError:
                    # 一个用户/一个市场的失败不该吞掉其余的：下一轮还会再来
                    logger.exception("%s/%s 这一趟盘中求值失败", user_id, market)
                    continue
                hits += len(report.raised)
        return IntradayRound(markets=tuple(live), rules=len(enabled), hits=hits)
