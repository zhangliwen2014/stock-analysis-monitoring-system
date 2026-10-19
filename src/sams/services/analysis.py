"""读取用例：把库里的三份事实拼成一次过闸的读数——一段区间的涨跌，或一条序列。

三份事实缺一样就不出数：K 线（首尾两根）、公司行为（跨除权日怎么折算）、覆盖确认
（谁在什么时候说过「这段区间的除权记录齐了」）。这个类的全部工作是**取数与定口径**，
判定本身在 `analysis`——反过来说，任何想拿到复权数的调用方都该走这里，
因为绕过这里通常意味着绕过某一道闸。

两个出口共用同一条取数路径（`interval_return` 与 `series`）：这不是代码复用，是
U20 那条要求的实现形态——指标与区间涨跌如果是两条各自取数的路，一道闸忘了装，
症状就是「复权涨跌对、复权指标错」，而这种组合恰好是最难被发现的（同一只票、
同一天、两个命令给两个答案）。

`market` 在入口就归一（`canonical_market`）：这是**第二重**——D50 之后仓库层写与读都按
同一形态落键，而 `daily_price_limit` 的板块判定也认大写。服务边界这重仍然留着，因为
D37 那道闸要的是「这个市场有没有涨跌幅证据」：拼写走不进那个分支时它不报错，
只是安静地不再工作。

`today` 一律取自注入的时钟，而不是请求窗口的末日：覆盖新鲜度与 PIT 上界都是
「今天看到的库里有什么」这件事的函数，让调用方用一次窄窗口买到一张免检票，
守卫就成了装饰。

上市首日（`listed_on`）按「显式给的 > 主档里那格 > 没有」取：它是 D37 那条「不可能
跳变」证据的替代品，所以**谁给的**必须跟着数一起出门（`listed_on_source`）——「库里
本来就写着」与「有人今天敲了一遍」的下一步完全不同。三处都没有就照 D56 拒绝，
不静默放宽那道闸。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Literal

import polars as pl

from sams.analysis.adjust import AdjustedSeries, adjusted_series
from sams.analysis.guard import ConfirmedSpan, is_covered
from sams.analysis.indicators import DEFAULT_INDICATORS, INDICATORS, metrics
from sams.analysis.resample import MONTH, WEEK, resample
from sams.analysis.returns import IntervalReturn, interval_return
from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import NotAdjustable, ValidationError
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.corp_actions_coverage import CorpActionsCoverageRepo
from sams.storage.repos.symbols import SymbolRepo

#: `resample()` 认的两个周期之外的唯一取值：日线不重采样，直接吃闸后那条序列。
#: 公开给命令行做 `--period` 的取值表（`cli` 不许 import 分析层，名单只能从这里转一道）
_DAY = "day"
PERIODS: tuple[str, ...] = (_DAY, WEEK, MONTH)

#: 豁免日期从哪来：命令行 `--listed-on` 传的，还是采集时填进主档的那一格（U18）。
#: 命令行不许 import 分析层，这两个名字与上面的 `PERIODS` 一样从服务层转一道
LISTED_ON_FROM_CLI: Literal["cli"] = "cli"
LISTED_ON_FROM_MASTER: Literal["master"] = "master"


@dataclass(frozen=True)
class SeriesRead:
    """一次「过闸 + 预热 + 算指标」的读数结果，连着它自己的口径。

    `warmup_floor` 是这套口径里唯一不自明的一项：预热能往前伸多远，由覆盖确认说了算
    （D36 管的就是「这几天有没有答案」）。它不是 None 时，表头的指标值可能因为前面
    根数不够而比理论上少——这不是错数，但必须说出来，否则读的人以为拿到的是全历史预热
    后的稳定值。
    """

    market: str
    code: str
    period: str
    #: 过闸的那条序列本身（重采样后的周期就是重采样结果）。
    #: `table` 是给屏幕的，这条是给下游算的：M2-D 的估值合成与 M4 的告警规则要吃的是
    #: 同一条序列，而不是同一张表的副本——U20 要的就是这一个出口
    series: AdjustedSeries
    #: 请求窗口内那张表：`date` 打头，接着是裸价/后复权收盘、成交量，再接指标列
    table: pl.DataFrame
    #: 请求的起日（原样，未挪动）
    requested_from: date
    requested_to: date
    #: 实际参与递推、但没打印出来的根数（早于 `requested_from` 的那些）
    warmup_bars: int
    #: 预热被这条覆盖确认截断在哪一天；None 表示一路铺到了库里最早那根
    warmup_floor: date | None
    #: 后复权倍数的锚：读取窗口的第一根。跨那次除权之前的因子不在窗口里，所以这条曲线的
    #: **绝对价位**只在这个锚以下可比，比值型指标（RSI/KDJ/WR）与均线相对本序列一致
    anchored_on: date
    covered_by: str
    covered_through: date
    ex_dates: int
    listed_on: date | None
    #: 表里最后一行是哪天：请求的止日常常比库里最新那根晚
    last_row: date | None

    #: 这次实际算了哪几组指标（`indicators=None` 时就是 `DEFAULT_INDICATORS`）。
    #: 命令行不许 import 分析层（`tests/test_layering.py`），所以能用哪些名字由这里转一道：
    #: `sams analysis series --indicators` 的取值表与报错时列的那份名单，来源都是这一个字段
    indicators: tuple[str, ...]

    #: 上面那个豁免日期的来源（`LISTED_ON_FROM_*`）。与 `listed_on` 同进同出：
    #: 「库里本来就写着」与「有人今天敲了一遍」是两句不同的话
    listed_on_source: str | None = None

    @property
    def rows(self) -> int:
        return self.table.height

    @property
    def pending_periods(self) -> list[date]:
        """表里还没走完的那几个周期（`--period week|month` 才可能有）。

        「这根周 K 只走了三天」在表上必须看得见：它与一根完整周 K 完全同形，
        而周线 MA5 吃的就是这根。
        """
        if "pending" not in self.table.columns:
            return []
        return list(self.table.filter(pl.col("pending"))["date"])


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

    def interval_return(
        self,
        market: str,
        code: str,
        start: date,
        end: date,
        *,
        listed_on: date | None = None,
    ) -> IntervalReturn:
        """[start, end] 的区间涨跌，裸价与复权一起给。拒绝时抛 `NotAdjustable` 带原因。

        代码先归一：`sh601088` 与 `601088` 是同一只票，而库里的键只有一个形态——
        拿别名查出来的空区间，在报告上长得和「这只票这段时间没行情」一模一样。

        `listed_on`（上市首日）按「显式给的 > 主档里那格 > 没有」取（U18）：它豁免掉的
        正是 D37 那条形证据，所以用了谁的那个数要跟着数一起打印。取不到仍然拒绝，
        不静默放宽那道闸（D56）。
        """
        market = canonical_market(market)
        code = canonical_code(market, code)
        self._require_ordered(start, end)
        today = self.clock.today()
        listed_on, listed_on_source = self._resolve_listed_on(market, code, listed_on)
        loaded = self.bars.load(market, [code], start=start, end=end)
        if loaded.height < 2:
            raise NotAdjustable(self._why_no_bars(market, code, start, end, loaded.height))
        result = interval_return(
            loaded,
            # PIT：晚于今天的除权日是还没发生的事，不能乘进今天看到的序列
            self.actions.list_for(market, code, until=today),
            market=market,
            code=code,
            start=start,
            end=end,
            coverage=self.coverage.for_code(market, code),
            today=today,
            listed_on=listed_on,
        )
        return replace(result, listed_on_source=listed_on_source)

    def series(
        self,
        market: str,
        code: str,
        *,
        start: date,
        end: date,
        period: str = _DAY,
        indicators: Sequence[str] | None = None,
        listed_on: date | None = None,
    ) -> SeriesRead:
        """一条过闸的复权序列 + 在它上面算好的指标（U20 的那个出口）。

        与 `interval_return` 走的是同一条取数路径、同一道闸：指标与区间涨跌必须来自
        同一个闸后面，否则「复权数」与「复权指标」会在某个调用方没注意的时候分家。

        读的是 `[预热下界, end]` 而不是 `[start, end]`：MA250 要 250 根、MACD 的 EMA
        从序列第一天起递推，只取请求那段的话，表头那一行的 MA20 是「用六根算的二十日线」
        之类的东西，而指标层的 `null` 规则（样本不足不给数）会把这种凑出来的值挡在
        请求段之外——前提是请求段之前真的有根。所以这里往回读，**读到覆盖确认允许的地方
        为止**（D36：多读的那几天同样要有答案），再往后就只按请求那段算。
        """
        market = canonical_market(market)
        code = canonical_code(market, code)
        self._require_ordered(start, end)
        if period not in PERIODS:
            raise ValidationError(
                f"周期只认 {', '.join(PERIODS)}，收到的是 {period!r}："
                "写成一个不认识的名字静默按日线算，等于给你一段不是你请求的行情"
            )
        # 先验指标名，再动库：拼错一个名字不应该让三道闸全部跑完才出声（架构评审第 9 项）。
        # `metrics()` 里同样有这道检查，这里是把它提前——两处口径相同，不在此重新造那套文案。
        names = tuple(dict.fromkeys(indicators)) if indicators else DEFAULT_INDICATORS
        unknown = [n for n in names if n not in INDICATORS]
        if unknown:
            raise ValidationError(
                f"不认识的指标名 {unknown}：可选的是 {', '.join(sorted(INDICATORS))}"
            )
        today = self.clock.today()
        listed_on, listed_on_source = self._resolve_listed_on(market, code, listed_on)
        loaded = self.bars.load(market, [code], end=end)
        in_window = loaded.filter(pl.col("date") >= start).height
        if in_window < 1:
            raise NotAdjustable(self._why_no_bars(market, code, start, end, in_window))
        spans = self.coverage.for_code(market, code)
        db_first = loaded["date"].min()
        read_from, floor = _warmup_span(spans, db_first=db_first, start=start, end=end)
        actions = self.actions.list_for(market, code, until=today)
        # D39 的预热修正：`_refuse_unpriceable` 的自救指引「起日 ≥ 该除权日」在 `series`
        # 这里不成立，因为 `_warmup_span` 把 `read_from` 推回覆盖起点，预热段可能横跨那条
        # 无法折算的记录。把 `read_from` 推到那条记录的除权日为止（`actions_in_span` 用的是
        # `> first`，等于该日那条 unknown 不再进 `inside`）；请求段本身仍由守卫兜住。
        if actions.height and "kind" in actions.columns:
            blockers = actions.filter(
                (pl.col("kind") == "unknown")
                & (pl.col("ex_date") > read_from)
                & (pl.col("ex_date") <= start)
            )
            if blockers.height:
                new_from = blockers["ex_date"].max()
                if new_from and new_from > read_from:
                    read_from = new_from
                    floor = new_from
        bars = loaded.filter(pl.col("date") >= read_from)
        series = adjusted_series(
            market,
            code,
            bars,
            actions,
            coverage=spans,
            today=today,
            listed_on=listed_on,
        )
        # 预热根数与锚都在**日线**口径上算：重采样后 `date` 是周期收市日标签，
        # 用它计预热会把"那一个月里前 7 天其实参与了递推"说成"0 根"（交易员闸第 2 项）。
        daily_dates = series.frame["date"].to_list()
        warmup_bars_count = sum(1 for d in daily_dates if d < start)
        anchor = daily_dates[0]
        if period != _DAY:
            series = resample(series, period, as_of=today)
        frame = series.frame
        # 裸价与后复权收盘并排给：读数的人拿裸价那列与行情终端对得上，拿指标列比形态。
        # `first`/`last`/`bars`/`pending` 只有重采样后的周期才有：那一根周 K 到底覆盖了
        # 哪几天、走完没有，是它在屏幕上唯一能自证的地方（周五休市那一周仍叫那个周五）
        keep = [
            c
            for c in ("close", "close_hfq", "volume", "pending", "first", "last", "bars")
            if c in frame.columns
        ]
        table = frame.select(["date", *keep]).hstack(metrics(series, names).drop("date"))
        rows = table.filter(pl.col("date") >= start)
        return SeriesRead(
            market=market,
            code=code,
            period=period,
            series=series,
            table=rows,
            indicators=names,
            requested_from=start,
            requested_to=end,
            warmup_bars=warmup_bars_count,
            warmup_floor=floor,
            anchored_on=anchor,
            covered_by=series.covered_by,
            covered_through=series.covered_through,
            ex_dates=series.ex_dates,
            listed_on=listed_on,
            listed_on_source=listed_on_source,
            last_row=rows["date"].max() if rows.height else None,
        )

    def _resolve_listed_on(
        self, market: str, code: str, given: date | None
    ) -> tuple[date | None, str | None]:
        """豁免日期从哪来：你给的 > 主档里那格 > 没有（U18）。

        * **显式的赢**：主档那一格可能是别的源填的、也可能属于一次敲错的代码，
          而 `--listed-on` 是这个人此刻对这只票说的一句话。
        * **主档已有就不再问源**：D37 的豁免窗口跟着这个日子走，换一家源就可能换一天。
        * **两处都没有 ⇒ 传 None**：守卫按「不豁免」处理，那条不可能跳变照样被拦下来。
          自动填的意义是少让人敲一遍，不是把那道闸改成默认敞开。
        """
        if given is not None:
            return given, LISTED_ON_FROM_CLI
        symbol = self.symbols.get(market, code)
        day = symbol.get("listed_on") if symbol is not None else None
        return (day, LISTED_ON_FROM_MASTER) if day is not None else (None, None)

    @staticmethod
    def _require_ordered(start: date, end: date) -> None:
        """反向区间在读库之前就拒：`start > end` 读回来是 0 根，而 0 根的那套文案会教人
        去跑一条同样空的 `sams sync daily --start … --end …`。下一步命令自己跑不通的拒绝，
        等于把用户钉在同一个敲错的参数上。
        """
        if start > end:
            raise ValidationError(
                f"起日 {start} 晚于止日 {end}：这是一个空区间，不是「这段时间没有行情」"
            )

    def _why_no_bars(self, market: str, code: str, start: date, end: date, in_window: int) -> str:
        """把「读不出能用的 K 线」拆成三种下一步不同的拒绝。

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
            f"{market}/{code} 在 {start}~{end} 内只有 {in_window} 根 K 线："
            f"全库有 {total} 根，最新到 {self.bars.latest_date(market, code)}。"
            f"把区间挪到有数据的那一段，或先跑 {sync_hint}"
        )


def _warmup_span(
    spans: Sequence[ConfirmedSpan], *, db_first: date, start: date, end: date
) -> tuple[date, date | None]:
    """预热往回读到哪一天，以及它是不是被覆盖确认截断了（返回 `(读到的最早一天, 截断处)`）。

    全历史确认只管上界（D49），所以整段都能拿来递推；只确认了某一小段时，往前那几天
    **没有答案**（D36），拿来算均线就是在用一条可能漏乘因子的曲线——多读的那几根
    不是「更精确」，而是把没有根据的那段乘进了表头那个数。
    """
    if is_covered(spans, start=db_first, end=end) is not None:
        return db_first, None
    span = is_covered(spans, start=start, end=end)
    if span is None:
        # 请求段自己就没确认：这道拒绝由守卫给（它带着「先跑 sync actions」那句下一步），
        # 这里不替它决定读哪一段
        return start, None
    floor = max(db_first, span.covered_from)
    return floor, floor if floor > db_first else None


def indicator_names() -> tuple[str, ...]:
    """`sams analysis series --indicators` 能点名的指标组。

    注册表在 `analysis.indicators`，而命令行不许 import 分析层（`tests/test_layering.py`），
    所以名单由服务层转一道：这样「有哪些名字」只有一个出处——`--help` 里列的那份、
    写错时报错列的那份、以及真在算的那份，是同一个东西。
    """
    return tuple(sorted(INDICATORS))


def default_indicators() -> tuple[str, ...]:
    """不给 `--indicators` 时算的那几组（表头会打印实际产出的列，不靠这句）。"""
    return DEFAULT_INDICATORS
