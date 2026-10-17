"""读取用例：把库里的三份事实拼成一次过闸的区间涨跌。

三份事实缺一样就不出数：K 线（首尾两根）、公司行为（跨除权日怎么折算）、覆盖确认
（谁在什么时候说过「这段区间的除权记录齐了」）。这个类的全部工作是**取数与定口径**，
判定本身在 `analysis`——反过来说，任何想拿到复权数的调用方都该走这里，
因为绕过这里通常意味着绕过某一道闸。

`today` 一律取自注入的时钟，而不是请求窗口的末日：覆盖新鲜度与 PIT 上界都是
「今天看到的库里有什么」这件事的函数，让调用方用一次窄窗口买到一张免检票，
守卫就成了装饰。

`listed_on`（上市首日）目前库里没有这一列，因此一律不传：守卫对上市初期的
不可能跳变因此按「不豁免」处理，多拒一次上市首日的暴涨。等主档补上来源再放开。
"""

from __future__ import annotations

from datetime import date

from sams.analysis.returns import IntervalReturn, interval_return
from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_code
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.corp_actions_coverage import CorpActionsCoverageRepo


class AnalysisService:
    def __init__(
        self,
        bars: BarRepo,
        actions: CorpActionRepo,
        coverage: CorpActionsCoverageRepo,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.bars = bars
        self.actions = actions
        self.coverage = coverage
        self.clock = clock

    def interval_return(self, market: str, code: str, start: date, end: date) -> IntervalReturn:
        """[start, end] 的区间涨跌，裸价与复权一起给。拒绝时抛 `NotAdjustable` 带原因。

        代码先归一：`sh601088` 与 `601088` 是同一只票，而库里的键只有一个形态——
        拿别名查出来的空区间，在报告上长得和「这只票这段时间没行情」一模一样。
        """
        code = canonical_code(market, code)
        today = self.clock.today()
        return interval_return(
            self.bars.load(market, [code], start=start, end=end),
            # PIT：晚于今天的除权日是还没发生的事，不能乘进今天看到的序列
            self.actions.list_for(market, code, until=today),
            market=market,
            code=code,
            start=start,
            end=end,
            coverage=self.coverage.for_code(market, code),
            today=today,
        )
