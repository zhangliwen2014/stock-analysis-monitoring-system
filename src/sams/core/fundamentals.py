"""基本面与估值数据的标准词汇与单位口径——采集、入库、算 PR 共用的同一套拼写。

放 core 的理由与 `core/actions.py` 一样：`datasource` 不许 import `storage`，而列名与
单位两侧必须一致。这里最贵的一种不一致不是报错，是**静默差两个数量级**：akshare 的
ROE/增速/股息率按百分数给（20.0 表示 20%），而市赚率写的算式是 `PR = PE ÷ ROE ÷ 100`
（D59）。按百分数存进去，PE=20、ROE=20% 会算出 PR=0.01，屏幕上它长得和「极度低估」
一模一样——一条会把所有高 ROE 的票排进「PR ≤ 0.4」前列的选股规则就是这么来的。

所以换算只发生在采集归一那一步（`from_percent`），库里只有小数口径这一种事实
（D58），读取侧不再有任何一处 `÷ 100`。
"""

from __future__ import annotations

#: 比率列：库里一律**小数**，0.20 = 20%，-0.05 = 下降 5%。源给百分数时在此 ÷100。
RATIO_COLUMNS = ("roe", "revenue_yoy", "profit_yoy", "dividend_yield")

#: 倍数列：20 倍市盈率就存 20.0，永远不参与 ÷100。与比率列搞混的后果见模块开头。
MULTIPLE_COLUMNS = ("pe_ttm", "pb")

#: 金额列：本币原值（A 股元、港股 HKD、美股 USD），不做单位归一；币种在 `symbols.currency`
MONEY_COLUMNS = ("market_cap", "main_net_in", "north_net", "north_hold", "margin_bal")

#: 进 `financials` 的列。粒度是**报告期**（主键 market/code/report_date）。
#: 注意这里刻意不含 `pe_ttm` / `pb`：那两列在 0001 的 DDL 里存在，但「今天的 PE」不属于
#: 任何一期报表，写进去就是拿一个日期冒充一个期间（D60）。按日的倍数走 `valuation_daily`。
FINANCIAL_COLUMNS = (
    "market",
    "code",
    "report_date",
    "published_date",
    "roe",
    "revenue_yoy",
    "profit_yoy",
    "dividend_yield",
    "market_cap",
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
TEXT_COLUMNS = ("market", "code", "source")

#: 百分数 → 小数的除数。写成常量而不是在两侧各写一个 `100.0`：口径换算的**数值**也只有一个出处。
PERCENT_DIVISOR = 100.0


def from_percent(value: float | None) -> float | None:
    """百分数 → 小数口径（D58）。None 原样交回，**不折成 0.0**。

    「股息率 0」与「不知道股息率」在选股里后果不同：前者是一只好票真的不分红，
    后者是源没给——把后者写成 0，「高股息」那条筛子就会把它静默排除掉。

    逐帧换算走 `datasource.fundamentals`（那里除数用的就是上面这个常量），
    这个标量版本留给源侧返回单个数字的场合（yfinance 的 info 字典）。
    """
    return None if value is None else value / PERCENT_DIVISOR


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
}


def out_of_caliber(column: str, value: float | None) -> bool:
    """这个数在 `column` 的小数口径里不可信（D58 + D66）——口径闸的唯一出处。

    写成函数而不是三处各写一遍 `abs(value) > bound`：仓库写入、命令行的比率参数、
    PR 的分母都在问同一个问题。漏掉的那一处的症状不是报错，是**同一个数在一侧被拦下、
    在另一侧被当成事实用掉**——`--roe-estimate 5`（= 5%）曾经就是这么走进 PR 的。
    """
    bound = RATIO_BOUNDS.get(column)
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


#: PR 的分母用哪一期 ROE（D62）。这一对拼写只有一个出处：命令行、仓库的 SQL、
#: 屏幕上的说明都引用它，漏改任何一处的症状都是「说明与算式不是同一个口径」。
ROE_BASIS_ANNUAL = "annual"
ROE_BASIS_LATEST = "latest"
ROE_BASIS = (ROE_BASIS_ANNUAL, ROE_BASIS_LATEST)

#: `MarketProfitRatio.basis` 的三个取值（D59）。词表放 core 而不是 analysis：命令行要按
#: 它分支措辞，而分层守卫不许 `cli` import `analysis`——把名单挪到 analysis 里等于
#: 让每个打印方自己重打一遍字符串，打错的那一处不会报错，只会把话说反。
BASIS_PE = "pe"
BASIS_PB_ESTIMATE = "pb_estimate"
BASIS_REFUSED = "refused"


def roe_basis_note(basis: str) -> str:
    """跟着 PR 出门的第二句口径话：分母那一期的**周期长度**。

    为什么单独立一条：源的 `ROEJQ` 是该期累计加权 ROE（Q1 是三个月、H1 是六个月），
    而 PE(TTM) 是滚动十二个月。实测 601088 在 PE≈20 不变时，按最新一期能给到
    PR=8.41（Q1）与 3.51（H1），按年报是 ~1.6——那个数主要在读「最近报的是哪个季度」。
    不说清这一条，`PR ≤ 0.4` 的排序就把「reporting 日历」当成了「便宜」。
    """
    if basis == ROE_BASIS_ANNUAL:
        return (
            "分母口径：ROE 取最新已披露年报（12-31 那一报告期）的加权值——十二个月，"
            "与 PE(TTM) 同周期；年报尚未披露时用的是上一份年报，可能滞后近一年"
        )
    return (
        "分母口径：ROE 取最新已披露一期的累计加权值——季报是三个月、半年报是六个月，"
        "而 PE(TTM) 是十二个月：这个 PR 会随最近报的是哪个季度而跳动，"
        "不能与年报口径的 PR 放在一篮子里排序"
    )
