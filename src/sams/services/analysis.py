"""读取用例：把库里的三份事实拼成一次过闸的区间涨跌。

三份事实缺一样就不出数：K 线（首尾两根）、公司行为（跨除权日怎么折算）、覆盖确认
（谁在什么时候说过「这段区间的除权记录齐了」）。这个类的全部工作是**取数与定口径**，
判定本身在 `analysis`——反过来说，任何想拿到复权数的调用方都该走这里，
因为绕过这里通常意味着绕过某一道闸。

`market` 在入口就归一（`canonical_market`）：这是**第二重**——D50 之后仓库层写与读都按
同一形态落键，而 `daily_price_limit` 的板块判定也认大写。服务边界这重仍然留着，因为
D37 那道闸要的是「这个市场有没有涨跌幅证据」：拼写走不进那个分支时它不报错，
只是安静地不再工作。

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
from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import NotAdjustable
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.corp_actions_coverage import CorpActionsCoverageRepo
from sams.storage.repos.symbols import SymbolRepo


class AnalysisService:
    def __init__(
        self,
        bars: BarRepo,
        actions: CorpActionRepo,
        coverage: CorpActionsCoverageRepo,
        symbols: SymbolRepo,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.bars = bars
        self.actions = actions
        self.coverage = coverage
        self.symbols = symbols
        self.clock = clock

    def interval_return(self, market: str, code: str, start: date, end: date) -> IntervalReturn:
        """[start, end] 的区间涨跌，裸价与复权一起给。拒绝时抛 `NotAdjustable` 带原因。

        代码先归一：`sh601088` 与 `601088` 是同一只票，而库里的键只有一个形态——
        拿别名查出来的空区间，在报告上长得和「这只票这段时间没行情」一模一样。
        """
        market = canonical_market(market)
        code = canonical_code(market, code)
        today = self.clock.today()
        loaded = self.bars.load(market, [code], start=start, end=end)
        if loaded.height < 2:
            raise NotAdjustable(self._why_no_bars(market, code, start, end, loaded.height))
        return interval_return(
            loaded,
            # PIT：晚于今天的除权日是还没发生的事，不能乘进今天看到的序列
            self.actions.list_for(market, code, until=today),
            market=market,
            code=code,
            start=start,
            end=end,
            coverage=self.coverage.for_code(market, code),
            today=today,
        )

    def _why_no_bars(self, market: str, code: str, start: date, end: date, in_window: int) -> str:
        """把「不足两根」拆成三种下一步不同的拒绝。

        「库里 0 行」这句话有三个原因：代码写错了、这只票从没采集过、采集了但这段没有
        行情。三种情况在报告上原本长得一模一样，而该敲的命令完全不同——主档有没有这个
        代码恰好是前两种的分界线，所以这里值得多问一次。
        """
        total = self.bars.count(market, code)
        sync_hint = (
            f"`sams sync daily --market {market} --code {code} --start {start} --end {end}`"
        )
        if total == 0:
            symbol = self.symbols.get(market, code)
            if symbol is None:
                return (
                    f"{market}/{code} 全库一根 K 线都没有，主档里也没有这个代码："
                    "代码多半写错了（指数、或没收录的标的也是这个形状）。"
                    f"确认代码后再跑 {sync_hint}"
                )
            return (
                f"{market}/{code}（{symbol['name'] or '未命名'}）在主档里，"
                f"但全库一根 K 线都没有：还没采集过。先跑 {sync_hint}"
            )
        return (
            f"{market}/{code} 在 {start}~{end} 内只有 {in_window} 根 K 线，首尾无从比较："
            f"全库有 {total} 根，最新到 {self.bars.latest_date(market, code)}。"
            f"把区间挪到有数据的那一段，或先跑 {sync_hint}"
        )
