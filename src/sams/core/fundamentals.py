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


def from_percent(value: float | None) -> float | None:
    """百分数 → 小数口径（D58）。None 原样交回，**不折成 0.0**。

    「股息率 0」与「不知道股息率」在选股里后果不同：前者是一只好票真的不分红，
    后者是源没给——把后者写成 0，「高股息」那条筛子就会把它静默排除掉。
    """
    return None if value is None else value / 100.0


def caliber_note() -> str:
    """那句要跟着数出门的口径声明（D54：算式与声明同口径才叫诚实）。

    写成函数而不是散在各命令里的字符串：改口径时只有一处可改，
    而漏改的那一处会在屏幕上继续宣布一个已经不成立的口径。
    """
    return (
        "口径：比率列一律小数（0.20 = 20%），PE/PB 是倍数，金额是本币原值；"
        "ROE 按报告期、PE/PB 按交易日，两个粒度各取各的截至日"
    )
