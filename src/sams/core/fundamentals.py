"""基本面与估值数据的标准词汇与单位口径——采集、入库、算 PR 共用的同一套拼写。

放 core 的理由与 `core/actions.py` 一样：`datasource` 不许 import `storage`，而列名与
单位两侧必须一致。这里最贵的一种不一致不是报错，是**静默差两个数量级**：akshare 的
ROE/增速/股息率按百分数给（20.0 表示 20%），而市赚率写的算式是 `PR = PE ÷ ROE ÷ 100`
（D59）。按百分数存进去，PE=20、ROE=20% 会算出 PR=0.01，屏幕上它长得和「极度低估」
一模一样——一条会把所有高 ROE 的票排进「PR ≤ 0.4」前列的选股规则就是这么来的。

所以换算只发生在采集归一那一步（`datasource.fundamentals.normalize_rows()`，除数是下面的
`PERCENT_DIVISOR`），库里只有小数口径这一种事实（D58），读取侧不再有任何一处 `÷ 100`。
"""

from __future__ import annotations

from datetime import date

#: 比率列：库里一律**小数**，0.20 = 20%，-0.05 = 下降 5%。源给百分数时在此 ÷100。
#: 后半段是 0028 的展示列：它们只给个股页看数，不进选股与市赚率。
RATIO_COLUMNS = (
    "roe",
    "revenue_yoy",
    "profit_yoy",
    "dividend_yield",
    "gross_margin",
    "net_margin",
    "roa",
    "roic",
    "debt_ratio",
)

#: 倍数列：20 倍市盈率就存 20.0，永远不参与 ÷100。与比率列搞混的后果见模块开头。
MULTIPLE_COLUMNS = ("pe_ttm", "pb")

#: 金额列：本币原值（A 股元、港股 HKD、美股 USD），不做单位归一；币种在 `symbols.currency`
MONEY_COLUMNS = (
    "market_cap",
    "main_net_in",
    "north_net",
    "north_hold",
    "margin_bal",
    "revenue",
    "net_profit",
)

#: 每股列：本币/股（0028 的展示列）。与金额列一样不换单位；屏幕上带币种名。
PER_SHARE_COLUMNS = ("eps", "bps", "ocf_per_share")

#: 倍数/周转列：倍或次（0028 的展示列），永远不参与 ÷100。
TURNOVER_COLUMNS = (
    "current_ratio",
    "quick_ratio",
    "inventory_turnover",
    "total_asset_turnover",
)

#: 进 `financials` 的列。粒度是**报告期**（主键 market/code/report_date）。
#: 注意这里刻意不含 `pe_ttm` / `pb`：那两列在 0001 的 DDL 里存在，但「今天的 PE」不属于
#: 任何一期报表，写进去就是拿一个日期冒充一个期间（D60）。按日的倍数走 `valuation_daily`。
FINANCIAL_COLUMNS = (
    "market",
    "code",
    "report_date",
    "published_date",
    "report_type",
    "roe",
    "revenue_yoy",
    "profit_yoy",
    "dividend_yield",
    "market_cap",
    "revenue",
    "net_profit",
    "eps",
    "bps",
    "ocf_per_share",
    "gross_margin",
    "net_margin",
    "roa",
    "roic",
    "debt_ratio",
    "current_ratio",
    "quick_ratio",
    "inventory_turnover",
    "total_asset_turnover",
    "source",
)

#: 进 `valuation_daily` 的列。粒度是**交易日**（主键 market/code/date）。
#: 股息率在这里是按日那个口径（源当日给的「股息率(TTM)」），与比率小数口径一致。
VALUATION_COLUMNS = (
    "market",
    "code",
    "date",
    "pe_ttm",
    "pb",
    "dividend_yield",
    "market_cap",
    "source",
)

#: 进 `funding_flow` 的列。粒度是交易日。
FLOW_COLUMNS = (
    "market",
    "code",
    "date",
    "main_net_in",
    "north_net",
    "north_hold",
    "margin_bal",
    "source",
)


#: 文本列：三张表共用的键列与来源列。类型判断只看这一份名单——采集侧与仓库侧各写一份
#: 「哪些列是文本」时，漏写的那一份会把 `market` 补成数值 null，然后在键归一那步炸掉。
TEXT_COLUMNS = ("market", "code", "source", "report_type")

#: 百分数 → 小数的除数。写成常量而不是在两侧各写一个 `100.0`：口径换算的**数值**也只有一个出处。
#:
#: 换算只有一处会发生：`datasource.fundamentals.normalize_rows()` 对着 `percent_columns`
#: 做那一次 `÷ PERCENT_DIVISOR`（D58）。这里刻意**没有**标量版的 `from_percent()`：
#: 它写出来时预想的消费者（yfinance 的 info 字典）最后压根没映射比率列——股息率那一列
#: 单位未核实，进库即 null（`akshare_cn` 给的是百分数，走的就是上面那一次）。
#: 留着它，「÷100 只有一处」这句话就要读的人自己去数有几个函数在除。
PERCENT_DIVISOR = 100.0


#: 小数口径下**可信的最大绝对值**（含边界：达到它就不可信）。越过它最可能的解释不是
#: 「这家公司 ROE 600%」，而是「百分数没换算就被存了下来」（D58）。
#: 仓库层的处置是**把那一列置空、留下这一行**：留着那个数，PR 会算出小两个数量级的
#: 「便宜」并把这只票排进低 PR 前列；置空则 PR 直接拒绝出数。两者之间选「不出数」。
#:
#: `roe` 的上界在 D66 从 5.0 收到 1.0。5.0 是按「500% 不可能」定的，却留下了 1.0–5.0
#: 这一段死区：没 ÷100 的「3.5%」正好落在里面，而 3.5% 恰恰是周期底部与困境反转票的
#: 真实 ROE 区间——最需要算 PR 的那批票，它们的百分数写法全在闸门之内，算出的 PR
#: 小两个数量级、方向永远是「极度便宜」。小数口径下 ROE 达到 100% 本身就不可信，
#: 而它与「忘了 ÷100」无法区分，所以一起拒。
#: 判据含边界（`abs(value) >= bound` 即不可信）：不含边界时 `roe=1.0`、
#: `dividend_yield=1.0` 这两个「同样不可能」的数正好从闸门下走过去。
#: 这条闸仍有已知的漏网：没换算的「营收 +0.5%」存成 0.005 也在上限内。它挡的是量级错，
#: 挡不住每一项具体数值——所以采集侧的真实用例仍然逐列断言区间。
RATIO_BOUNDS = {
    "roe": 1.0,
    "revenue_yoy": 5.0,
    "profit_yoy": 5.0,
    "dividend_yield": 1.0,
    # 0028 的展示列：界设得宽，只挡「百分数没 ÷100」那种量级错，不挡真实的异常值
    "gross_margin": 2.0,
    "net_margin": 20.0,
    "roa": 5.0,
    "roic": 10.0,
    "debt_ratio": 2.0,
}


#: 个别市场里 100% 以上的 ROE 是**真实值**（D228）：美股回购把股东权益压得很小，
#: 苹果 2025 年报 ROE=171.4%。默认的 1.0 是为「百分数没 ÷100」设的，在美股会把真实值
#: 静默置空。放宽只针对具体市场与具体列，默认闸不动；上界仍留 5.0——171.4、35.0
#: 这种真正没除数的写法照样被拦下。
MARKET_RATIO_BOUNDS: dict[tuple[str, str], float] = {("US", "roe"): 5.0}


def ratio_bound(column: str, *, market: str | None = None) -> float | None:
    """这一列在**这个市场**的可信上界：先看市场覆盖，再落到默认闸。"""
    if market is not None:
        override = MARKET_RATIO_BOUNDS.get((market, column))
        if override is not None:
            return override
    return RATIO_BOUNDS.get(column)


def out_of_caliber(column: str, value: float | None, *, market: str | None = None) -> bool:
    """这个数在 `column` 的小数口径里不可信（D58 + D66 + D228）——口径闸的唯一出处。

    写成函数而不是三处各写一遍 `abs(value) > bound`：仓库写入、命令行的比率参数、
    PR 的分母都在问同一个问题。漏掉的那一处的症状不是报错，是**同一个数在一侧被拦下、
    在另一侧被当成事实用掉**——`--roe-estimate 5`（= 5%）曾经就是这么走进 PR 的。

    不传 `market` 时用默认上界；入库侧按行把市场带进来，才能让美股的真实高 ROE 过闸
    而 A 股那一段死区仍然关闭。
    """
    bound = ratio_bound(column, market=market)
    if bound is None or value is None:
        return False
    return abs(value) >= bound


def caliber_note() -> str:
    """那句要跟着数出门的口径声明（D54：算式与声明同口径才叫诚实）。

    写成函数而不是散在各命令里的字符串：改口径时只有一处可改，
    而漏改的那一处会在屏幕上继续宣布一个已经不成立的口径。
    """
    return (
        "口径：比率列一律小数（0.20 = 20%），PE/PB 是倍数，金额是本币原值；"
        "ROE 按报告期、PE/PB 按交易日，两个粒度各取各的截至日"
    )


#: PR 与门槛比较时的精度：门槛写在命令行上，就要能被正好敲中。
#: 这个数也是「门槛至少要多大」的判据——round 成 0 的门槛意思是「只收免费的票」，
#: 而 0 在 PR 的刻度上是最贵的一个误读，所以它必须与比较用的小数位同出处（D66）。
PR_SCALE_DECIMALS = 6


#: 按日估值算「还新」的最大天数（日历日，D64）。读 PE 与按 PE 选股在问同一个问题，
#: 就该拿同一个答案：两处各写一个字面量时，同一个 PE 会在一个命令里叫「旧过」、
#: 在另一个里叫「新鲜」，而屏幕上两句都像实话。
#:
#: 7 而不是更大：按日估值停在几天前，最常见的解释是那只票这几天停牌——而停牌期间的
#: PE 不是市场现在的出价。周末两天 + 一个假期就吃掉 3 天，所以余量本来就小。
STALE_VALUATION_DAYS = 7


#: PR 的分母用哪一期 ROE（D62）。这一对拼写只有一个出处：命令行、仓库的 SQL、
#: 屏幕上的说明都引用它，漏改任何一处的症状都是「说明与算式不是同一个口径」。
ROE_BASIS_ANNUAL = "annual"
ROE_BASIS_LATEST = "latest"
ROE_BASIS = (ROE_BASIS_ANNUAL, ROE_BASIS_LATEST)

#: 财年不按 12-31 收尾的市场（D228）：美股公司可以 9 月末、6 月末结账，苹果的
#: 2025 年报报告期是 2025-09-27。这些市场的「年报」认源给的报告类型（`report_type`），
#: 不认日历日；A 股与港股仍按 12-31 判（D62 的那条结论）。
FISCAL_YEAR_ANNUAL_MARKETS = frozenset({"US"})

#: 财务源根本不提供披露日的市场（D215 港股指标源 / D228 美股 F10）：这些行按
#: `report_date <= as_of` 采信，但同一只票已有带披露日的行时，带日期的那期优先。
#: 名单只在这里写一次：读侧的开口（`load_as_of(include_undated=…)`）与口径话
#: （`roe_basis_note`）都引用它，漏掉一处就会出现「web 算得出、CLI 算不出」。
UNDATED_FINANCIAL_MARKETS = frozenset({"HK", "US"})

#: `MarketProfitRatio.basis` 的三个取值（D59）。词表放 core 而不是 analysis：命令行要按
#: 它分支措辞，而分层守卫不许 `cli` import `analysis`——把名单挪到 analysis 里等于
#: 让每个打印方自己重打一遍字符串，打错的那一处不会报错，只会把话说反。
BASIS_PE = "pe"
BASIS_PB_ESTIMATE = "pb_estimate"
BASIS_REFUSED = "refused"


def is_year_end(day: date | None) -> bool:
    """这一报告期是不是「年报」那一期的**唯一拼写**（D62）。

    打印「年报 / 3 月末期」的两个出口各自判一次时，判据就有两份：改一次只改到一处，
    漏掉的那一处会继续把季报叫成年报，而 `--roe-basis annual` 那句话讲的正是这两个字。
    入库侧的同一句判据写成 SQL（`storage/repos/financials.py` 的口径子句），
    因为「哪一期参选」必须在库里判；两处改的时候一起改。
    """
    return day is not None and day.month == 12 and day.day == 31


def period_label(day: date, market: str, *, basis: str = ROE_BASIS_ANNUAL) -> str:
    """报告那一期的名字：`年报` / `年报（财年止 09-27）` / `N 月末期`。

    年报口径下美股那一期是源标为「年报」的行（D228），报告期可以不是 12-31；
    按日历日叫它「9 月末期」，读的人会以为 PR 的分母是三季报。
    """
    if basis == ROE_BASIS_ANNUAL and market in FISCAL_YEAR_ANNUAL_MARKETS:
        return "年报" if is_year_end(day) else f"年报（财年止 {day:%m-%d}）"
    return "年报" if is_year_end(day) else f"{day.month} 月末期"


def roe_basis_note(basis: str, market: str | None = None) -> str:
    """跟着 PR 出门的第二句口径话：分母那一期的**周期长度**与采信条件。

    为什么单独立一条：源的 `ROEJQ` 是该期累计加权 ROE（Q1 是三个月、H1 是六个月），
    而 PE(TTM) 是滚动十二个月。实测 601088 在 PE≈20 不变时，按最新一期能给到
    PR=8.41（Q1）与 3.51（H1），按年报是 ~1.6——那个数主要在读「最近报的是哪个季度」。
    不说清这一条，`PR ≤ 0.4` 的排序就把「reporting 日历」当成了「便宜」。

    `market` 决定另外两句（都是「这句话不写就是反话」的那类）：

    * 美股的年报不是 12-31（D228），按日历日说会让人以为取错了期；
    * 港美股的源不提供披露日（D215/D228），按报告期采信是口径而不是缺口。
    """
    if basis == ROE_BASIS_ANNUAL:
        annual = (
            f"ROE 取最新已披露年报（源标为「年报」的那一期；{market} 的财年止日"
            "可以不是 12-31）"
            if market in FISCAL_YEAR_ANNUAL_MARKETS
            else "ROE 取最新已披露年报（12-31 那一报告期）"
        )
        note = (
            f"分母口径：{annual}的加权值——十二个月，"
            "与 PE(TTM) 同周期；年报尚未披露时用的是上一份年报，可能滞后近一年"
        )
    else:
        note = (
            "分母口径：ROE 取最新已披露一期的累计加权值——季报是三个月、半年报是六个月，"
            "而 PE(TTM) 是十二个月：这个 PR 会随最近报的是哪个季度而跳动，"
            "不能与年报口径的 PR 放在一篮子里排序"
        )
    if market in UNDATED_FINANCIAL_MARKETS:
        note += (
            f"；{market} 的财务源不提供披露日（D215/D228）："
            "该期按「报告期 ≤ 截至日」采信，公开时点无从核对"
        )
    return note
