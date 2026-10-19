"""A 股主源：akshare（底层封装东方财富）。

抓取函数可注入（`fetch`），因此解析与规范化路径能在无网络环境下被测试。
这是本项目所有适配器的统一形态：IO 边界注入，纯逻辑直测。

同一个市场在这里给了四条链（日线、公司行为、基本面、按日估值、资金流），但它们是
**四个不同的接口、四种键写法**：`stock_zh_a_hist` 要 `601088`、`..._indicator_em` 要
`601088.SH`、`stock_individual_fund_flow` 要 `market="sh"`。写错的那一种不会报错，
只会返回另一只票或者返回空——所以归一规则在 `secucode` / `fund_flow_market` 里一处写明，
并被测试逐条钉住。

**单位在这个市场内部就是分裂的**（D58 最难的一处）：财务主要指标给的 ROE/增速是百分数
（`ROEJQ=5.83` 表示 5.83%），而分红送配表给的 `现金分红-股息率` 已经是小数（`0.055055`
表示 5.5%）。所以 `financials()` 的 `percent_columns` 里**没有** `dividend_yield`——
把它一起 ÷100，屏幕上会出现「股息率 0.06%」这样一行看起来像数据而不是 bug 的错数。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

import polars as pl

from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift
from sams.datasource.actions import empty_actions, normalize_actions
from sams.datasource.akshare_actions import expand_fhps
from sams.datasource.base import (
    LOTS_TO_SHARES,
    a_share_symbol,
    classify_error,
    code_in_market,
    normalize_bars,
    to_date_expr,
)
from sams.datasource.fundamentals import (
    empty_financials,
    empty_flow,
    empty_valuation,
    normalize_financials,
    normalize_flow,
    normalize_valuation,
)
from sams.datasource.sources import source

logger = logging.getLogger("sams.datasource")

COLUMN_MAP = {
    "date": "日期",
    "open": "开盘",
    "high": "最高",
    "low": "最低",
    "close": "收盘",
    "volume": "成交量",
    "amount": "成交额",
}

#: akshare 把请求的代码回写在每一行里，拿它确认取到的确实是那只票
ECHO_COLUMN = "股票代码"

#: 分红送配表里那两列：报告期 + **已经是小数**的股息率
_FHPS_PERIOD = "报告期"
_FHPS_YIELD = "现金分红-股息率"
#: 并到财务指标上的中间列名：与 `FINANCIAL_MAP` 的键对应，归一后被重命名成 `dividend_yield`
YIELD_COLUMN = "股息率"


def require_echoed_code(frame: pl.DataFrame, *, code: str) -> None:
    """回写的代码与请求不一致 → 整批都不能要。

    换错票不会露出破绽：`000001` 的曲线挂到 `600519` 名下，看起来只是一只跌得很难看的
    茅台，而它已经是库里的事实，事后无从查出。响应里没有这一列时不拒绝——没证据不等于矛盾。
    """
    if ECHO_COLUMN not in frame.columns:
        return
    echoed = {str(value) for value in frame[ECHO_COLUMN].unique().to_list()}
    if echoed != {code}:
        raise FieldDrift("akshare", f"响应回写的代码是 {sorted(echoed)}，请求的是 {code}")


#: 北交所号段。`base.a_share_symbol` 只分沪/深（新浪与腾讯要的那个形态），而东财的资金流
#: 接口要第三种标签：把 43/83/87/920 开头的票当深市问一次，接口回空——空看起来像
#: 「这只票没有资金流记录」，而真相是问错了交易所。
_BJ_PREFIXES = ("43", "83", "87", "920")
#: `secucode` 的后缀由交易所标签推出来：两处共用一份判据，别各写一遍号段规则
_SUFFIX = {"sh": "SH", "sz": "SZ", "bj": "BJ"}


def fund_flow_market(code: str) -> str:
    """东财资金流接口的市场标签：沪 sh、深 sz、北 bj。"""
    symbol = code_in_market("A", code)
    if symbol.startswith(_BJ_PREFIXES):
        return "bj"
    return a_share_symbol(symbol)[:2]


def secucode(market: str, code: str) -> str:
    """东财 F10 那一族的代码形态：`601088.SH`。给它裸代码回的是空表。"""
    symbol = code_in_market(market, code)
    return f"{symbol}.{_SUFFIX[fund_flow_market(symbol)]}"


def _require_market(adapter: str, markets: frozenset[str], market: str) -> None:
    if market not in markets:
        raise ValueError(f"{adapter} 不支持市场 {market}")


def _require_columns(needed: tuple[str, ...], raw: pl.DataFrame, *, table: str) -> None:
    """值列整列消失 = 接口改版，当场失败。

    为什么不填 null 继续走：`ROEJQ` 没了会写出一库空的 ROE，读取侧的结论是
    「这只票 ROE 缺失，PR 算不出」——那句结论是假的，真相是采集把列弄丢了。
    """
    absent = [column for column in needed if column not in raw.columns]
    if absent:
        raise FieldDrift(
            "akshare", f"{table} 缺少必需列 {absent}，实际列 {list(raw.columns)}"
        )


def _in_window(frame: pl.DataFrame, column: str, start: date, end: date) -> pl.DataFrame:
    """按**该表自己的粒度列**切窗口：基本面切报告期，估值与资金流切交易日（D60）。

    这几个东财接口都没有日期参数，一次给全史。不切的话，`sync valuation --start 2024`
    会写进 2007 年以来的每一行，而屏幕上那句「写入 2 行」就成了假话。
    """
    if frame.is_empty():
        return frame
    return frame.filter(pl.col(column).is_between(start, end))


def akshare_fetch(symbol: str, start: date, end: date) -> pl.DataFrame:
    """真实网络调用。列名为中文，与 akshare.stock_zh_a_hist 一致。"""
    import akshare as ak

    frame = ak.stock_zh_a_hist(
        symbol=symbol,
        period="daily",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        adjust="",
    )
    if frame is None or len(frame) == 0:
        raise EmptyResponse("akshare", f"{symbol} 区间无数据")
    return pl.from_pandas(frame)


def eastmoney_says_no_records(symbol: str) -> bool:
    """直接问一次东财：这只票的分红送配表是不是「查无记录」（只在崩了之后走这一趟）。

    akshare 的 `stock_fhps_detail_em` 在 `data_json["result"]` 为 null 时拿 None 做下标，
    抛 `TypeError: 'NoneType' object is not subscriptable`（实测 002450、600636，而这两个
    代码在表里确实没有记录，同花顺各有 8~28 条）。崩在它自己的解析里，所以「东财回查无记录」
    与「接口改版/别的 None」在异常上长得一模一样。两者要的下一步相反：前者应当继续问下一个源
    并把这句当成一句答复，后者应当报警。多花一个请求换这个区分，只在失败路径上付。
    """
    import requests

    try:
        response = requests.get(
            "https://datacenter-web.eastmoney.com/api/data/v1/get",
            params={
                "sortColumns": "REPORT_DATE",
                "sortTypes": "-1",
                "pageSize": "500",
                "pageNumber": "1",
                "reportName": "RPT_SHAREBONUS_DET",
                "columns": "ALL",
                "quoteColumns": "",
                "js": '{"data":(x),"pages":(tp)}',
                "source": "WEB",
                "client": "WEB",
                "filter": f'(SECURITY_CODE="{symbol}")',
            },
            timeout=15,
        )
        body = response.json()
    except Exception:  # noqa: BLE001 — 问不出结论就退回「这不是查无记录」，让原异常说话
        return False
    return body.get("result") is None and str(body.get("code")) == "9201"


def akshare_fhps(
    symbol: str, *, is_no_records: Callable[[str], bool] = eastmoney_says_no_records
) -> pl.DataFrame:
    """真实网络调用：某只 A 股的分红送配**全历史**（东财明细页）。

    这个接口不带日期参数，一次给全史——所以适配器标了 `corp_actions_full_history`（D36），
    而「给了全史」与「这一行是 0 条」是两件事，后者要区分「从没分过红」和「没查过」。
    另外它**不回写代码**（列里没有股票代码），换错票不会在任何一列上露出破绽：
    代码只能信任请求参数，因此这里坚持先 `code_in_market` 归一再传入。
    """
    import akshare as ak

    try:
        frame = ak.stock_fhps_detail_em(symbol=symbol)
    except TypeError as exc:
        if not is_no_records(symbol):
            raise
        # 「这张表里没有这只票的记录」：先当一句答复交回空帧，让降级链继续问下一个源。
        # 说成故障会烧掉 akshare 的健康预算，说成答复又会让覆盖标记撒谎——两边都不行，
        # 所以这里只回空帧，写不写覆盖由服务层按「有没有源真的答上来」决定（D36）。
        logger.info("东财分红送配对 %s 回「返回数据为空」，换下一个源（%s）", symbol, exc)
        return pl.DataFrame()
    if frame is None or len(frame) == 0:
        # 一次都没有过分红是一家真实存在的公司的正常状态，不是故障：返回空帧，
        # 让服务层把「查过了、没有」写成覆盖标记（D36）
        return pl.DataFrame()
    return pl.from_pandas(frame)


#: 财务主要指标（东财 F10）：列名是东财自己的缩写，实测 141 列，这里只取六列
FINANCIAL_MAP = {
    "report_date": "REPORT_DATE",
    "published_date": "NOTICE_DATE",
    "roe": "ROEJQ",
    "revenue_yoy": "TOTALOPERATEREVETZ",
    "profit_yoy": "PARENTNETPROFITTZ",
    "dividend_yield": YIELD_COLUMN,
}
#: **只有这三列 ÷100**。`dividend_yield` 刻意不在内：它来自分红送配表，那一列本来就是小数
#: （模块开头那条）。把它们一起声明成百分数，股息率会静默小一百倍。
FINANCIAL_PERCENT = ("roe", "revenue_yoy", "profit_yoy")
#: 值列：整列消失按改版处理。`NOTICE_DATE` 不在内——它缺了那些 ROE 仍是真数，
#: 由 `normalize_rows` 出声告警，而 PIT 读取会自己把没有披露日的那些期挡掉（D61）
FINANCIAL_REQUIRED = ("REPORT_DATE", "ROEJQ", "TOTALOPERATEREVETZ", "PARENTNETPROFITTZ")

#: 每日估值（东财估值分析页）：全历史按交易日，倍数与市值都有，**没有股息率**
VALUATION_MAP = {
    "date": "数据日期",
    "pe_ttm": "PE(TTM)",
    "pb": "市净率",
    "market_cap": "总市值",
}
VALUATION_REQUIRED = ("PE(TTM)", "市净率")

#: 个股资金流：只有主力净流入这一件事是可得的。北向持股、融资余额在另外的表里，
#: 本轮没有接——列留着填 null，比拿 0 说「今天没有北向」诚实
FLOW_MAP = {"date": "日期", "main_net_in": "主力净流入-净额"}
FLOW_REQUIRED = ("主力净流入-净额",)


def akshare_financials_indicator(symbol: str) -> pl.DataFrame:
    """真实网络调用：某只 A 股的财务主要指标（按报告期，全历史）。

    `symbol` 是 `601088.SH` 形态。接口没有日期参数，一次回 200 期。
    """
    import akshare as ak

    frame = ak.stock_financial_analysis_indicator_em(symbol=symbol, indicator="按报告期")
    if frame is None or len(frame) == 0:
        return pl.DataFrame()
    return pl.from_pandas(frame)


def akshare_valuation(symbol: str) -> pl.DataFrame:
    """真实网络调用：某只 A 股的按日估值（PE/PB/总市值，自上市以来）。`symbol` 是裸代码。"""
    import akshare as ak

    frame = ak.stock_value_em(symbol=symbol)
    if frame is None or len(frame) == 0:
        return pl.DataFrame()
    return pl.from_pandas(frame)


#: 上市日期（U18）：东财 F10 的公司概况里有 `LISTING_DATE`，而 akshare 把同一件事包在
#: `stock_individual_info_em` 里——那个接口打 `push2.eastmoney.com` 的行情快照，本环境实测
#: 不可达（`ConnectionError: Remote end closed connection without response`）。报告接口
#: （`datacenter.eastmoney.com/securities`，与 `eastmoney_says_no_records` 同一族）给的是
#: 同一个日子：实测 601088 → 2007-10-09。
LISTING_REQUIRED = ("LISTING_DATE",)
#: 东财报告接口里「这张表里没有这条记录」的那个码（与分红送配表同一个约定）。
#: 别的 `success: false` 是接口在抱怨（`9501 报表配置不存在`），不能当成一句答复
_NO_RECORD_CODE = 9201


def parse_listing_response(body: dict, *, symbol: str) -> pl.DataFrame:
    """报告接口的两种「没有数据」分开处理：查无记录是答复，报表不存在是改版。

    把后者也咽成空帧，主档那一格就永远停在「没有」而屏幕上不说为什么——
    D43 在基本面三链上分的那一刀，在这里同样要分。
    """
    if not body.get("success", True):
        code = body.get("code")
        if code == _NO_RECORD_CODE:
            return pl.DataFrame()
        raise FieldDrift(
            "akshare",
            f"{symbol} 的公司概况被报告接口拒绝了：code={code} message={body.get('message')}",
        )
    rows = (body.get("result") or {}).get("data") or []
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows)


def akshare_listing_info(symbol: str) -> pl.DataFrame:
    """真实网络调用：某只 A 股的公司概况（只要上市日期那一列）。`symbol` 是 `601088.SH`。"""
    import requests

    response = requests.get(
        "https://datacenter.eastmoney.com/securities/api/data/v1/get",
        params={
            "reportName": "RPT_F10_BASIC_ORGINFO",
            "columns": "SECUCODE,LISTING_DATE",
            "filter": f'(SECUCODE="{symbol}")',
            "pageNumber": "1",
            "pageSize": "1",
            "source": "F10",
            "client": "PC",
        },
        timeout=15,
    )
    return parse_listing_response(response.json(), symbol=symbol)


def akshare_flow(symbol: str, market_tag: str) -> pl.DataFrame:
    """真实网络调用：某只 A 股的按日资金流。`market_tag` 取自 `fund_flow_market`。"""
    import akshare as ak

    frame = ak.stock_individual_fund_flow(stock=symbol, market=market_tag)
    if frame is None or len(frame) == 0:
        return pl.DataFrame()
    return pl.from_pandas(frame)


def attach_dividend_yield(indicators: pl.DataFrame, payouts: pl.DataFrame) -> pl.DataFrame:
    """把分红送配表里的股息率按**报告期**并到财务指标上，产出一列 `股息率`。

    为什么要并：A 股免费的按日股息率在这一版 akshare 里不存在（`stock_a_indicator_lg`
    已被移除，东财估值表不给股息率），而用户要的「分红率」得有出处。分红表给的是
    **该期分红方案对应的股息率**（源按除权日前价算），粒度与报告期一致，所以它进
    `financials` 而不进 `valuation_daily`（D60）——拿一个期间的数冒充某一天的数，
    正是这一轮拆表时要消灭的那件事。

    同一报告期有多条方案（中期与年度各自成行）时取源里最后一条，并与 `normalize_rows`
    的「同键保留最后一条」一致；两批行都不是「没有股息率」的意思。
    """
    if (
        payouts.is_empty()
        or _FHPS_PERIOD not in payouts.columns
        or _FHPS_YIELD not in payouts.columns
    ):
        return indicators.with_columns(pl.lit(None, dtype=pl.Float64).alias(YIELD_COLUMN))
    rates = (
        payouts.select(
            to_date_expr(pl.col(_FHPS_PERIOD)).alias("period"),
            pl.col(_FHPS_YIELD).cast(pl.Float64, strict=False).alias(YIELD_COLUMN),
        )
        .filter(pl.col("period").is_not_null() & pl.col(YIELD_COLUMN).is_not_null())
        .unique(subset=["period"], keep="last")
        .sort("period")
    )
    return (
        indicators.with_columns(to_date_expr(pl.col("REPORT_DATE")).alias("period"))
        .join(rates, on="period", how="left")
        .drop("period")
    )


class AkshareAdapter:
    name = "akshare"
    markets = frozenset({"A"})
    #: 分红送配接口不带日期参数，一次给全史，所以确认能做到「截至今天的全部历史」
    corp_actions_full_history = True

    def __init__(
        self,
        fetch: Callable[[str, date, date], pl.DataFrame] = akshare_fetch,
        fhps_fetch: Callable[[str], pl.DataFrame] = akshare_fhps,
        financials_fetch: Callable[[str], pl.DataFrame] = akshare_financials_indicator,
        valuation_fetch: Callable[[str], pl.DataFrame] = akshare_valuation,
        flow_fetch: Callable[[str, str], pl.DataFrame] = akshare_flow,
        listing_fetch: Callable[[str], pl.DataFrame] = akshare_listing_info,
    ) -> None:
        self._fetch = fetch
        self._fhps_fetch = fhps_fetch
        self._financials_fetch = financials_fetch
        self._valuation_fetch = valuation_fetch
        self._flow_fetch = flow_fetch
        self._listing_fetch = listing_fetch

    def corp_actions(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        """A 股分红送配。源没有日期参数，一次给全史——那就把全史都交回去，**不按窗口切**。

        窗口切片看着只是少存几行，实际是让库与覆盖标记说反话：服务层会写下
        「全历史，确认至今天」，而库里缺着窗口外那些真的除权日。守卫照着覆盖标记放行，
        老日子的区间涨跌就变成裸价——正是 U12 那个 −3.81%。
        """
        _require_market(self.name, self.markets, market)
        symbol = code_in_market(market, code)
        try:
            raw = _as_polars(self._fhps_fetch(symbol))
            if raw.is_empty():
                return empty_actions()
            expanded = expand_fhps(raw)
            if expanded.is_empty():
                return empty_actions()
            actions = normalize_actions(expanded, market=market, code=code, source=self.name)
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001 — 边界处统一归类，见 classify_error
            raise classify_error(self.name, exc) from exc
        return actions

    def daily_bars(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        _require_market(self.name, self.markets, market)
        symbol = code_in_market(market, code)
        try:
            raw = _as_polars(self._fetch(symbol, start, end))
            if raw.is_empty():
                raise EmptyResponse(self.name, f"{market} {code} 区间无数据")
            require_echoed_code(raw, code=symbol)
            bars = normalize_bars(
                raw,
                market=market,
                code=code,
                source=self.name,
                column_map=COLUMN_MAP,
                volume_scale=LOTS_TO_SHARES,
            )
        except DatasourceError:
            raise  # 已经是本域异常（EmptyResponse / FieldDrift），别二次归类
        except Exception as exc:  # noqa: BLE001 — 边界处统一归类，见 classify_error
            raise classify_error(self.name, exc) from exc
        return bars

    def financials(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        """按报告期的财务指标：ROE、营收与利润增速、披露日，股息率从分红送配表并进来。

        分红表这一次调用**不是顺手多要的一列**：它失败时整条基本面跟着失败。理由是
        没有别的处可说——`FrameResult` 里没有一个「部分成功」的字段，把失败咽下来只会
        让库里出现一列空的股息率，而屏幕上那格显示的「未知」看起来像源给的答案，
        不像我们没问到。宁可让链换源或出声，也不发一句半真半话的确认。
        """
        _require_market(self.name, self.markets, market)
        symbol = code_in_market(market, code)
        try:
            raw = _as_polars(self._financials_fetch(secucode(market, code)))
            if raw.is_empty():
                return empty_financials()
            _require_columns(FINANCIAL_REQUIRED, raw, table="财务主要指标")
            merged = attach_dividend_yield(raw, _as_polars(self._fhps_fetch(symbol)))
            out = normalize_financials(
                merged,
                market=market,
                code=code,
                source=self.name,
                column_map=FINANCIAL_MAP,
                percent_columns=FINANCIAL_PERCENT,
            )
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc
        return _in_window(out, "report_date", start, end)

    def valuation(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        """按交易日的 PE(TTM)/PB/市值。市赚率要的那个「今天的 PE」从这里来（D60）。

        亏损票的负 PE 原样交回：把它擦成 null，PR 就走不到「PB 配预估 ROE」那一支（D59），
        而困境反转股正是这条规则最想找出来的那类票。
        """
        _require_market(self.name, self.markets, market)
        symbol = code_in_market(market, code)
        try:
            raw = _as_polars(self._valuation_fetch(symbol))
            if raw.is_empty():
                return empty_valuation()
            _require_columns(VALUATION_REQUIRED, raw, table="每日估值")
            out = normalize_valuation(
                raw,
                market=market,
                code=code,
                source=self.name,
                column_map=VALUATION_MAP,
                percent_columns=(),  # 这一族给的全是倍数与金额，没有比率列
            )
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc
        return _in_window(out, "date", start, end)

    def funding_flow(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        """按交易日的主力净流入。北向与融资余额这个源给不出，留 null。"""
        _require_market(self.name, self.markets, market)
        symbol = code_in_market(market, code)
        try:
            raw = _as_polars(self._flow_fetch(symbol, fund_flow_market(symbol)))
            if raw.is_empty():
                return empty_flow()
            _require_columns(FLOW_REQUIRED, raw, table="个股资金流")
            out = normalize_flow(
                raw,
                market=market,
                code=code,
                source=self.name,
                column_map=FLOW_MAP,
                percent_columns=(),  # 全是金额
            )
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc
        return _in_window(out, "date", start, end)

    def listed_date(self, market: str, code: str) -> date | None:
        """上市首日（U18）：D37 那条上市豁免的机器来源。

        `None` 是「这个源没有这只票的上市日」，不是故障——抛出去的那句才是「答不上来」，
        两者在主档那一格上的下一步相反（见 `datasource/listed_date.py` 的文件头）。
        也不拿「库里最早那根 K 线」冒充：那是采集起点，不是上市日。
        """
        _require_market(self.name, self.markets, market)
        try:
            raw = _as_polars(self._listing_fetch(secucode(market, code)))
            if raw.is_empty():
                return None
            _require_columns(LISTING_REQUIRED, raw, table="公司概况的上市日期")
            day = raw.select(to_date_expr(pl.col("LISTING_DATE")).alias("listed_on"))[
                "listed_on"
            ][0]
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc
        return day


def _as_polars(frame) -> pl.DataFrame:
    if isinstance(frame, pl.DataFrame):
        return frame
    return pl.from_pandas(frame)


# 注册进 datasource.sources：加源只改自己这一行，不动 registry / bootstrap / CLI
source(
    "akshare",
    note=(
        "akshare A 股主源（日线 + 分红送配 + 基本面 + 按日估值 + 资金流 + 上市日期，"
        "底层都是东财）"
    ),
)(AkshareAdapter)
