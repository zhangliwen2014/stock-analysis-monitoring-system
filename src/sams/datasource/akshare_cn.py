"""A 股主源：akshare。

抓取函数可注入（`fetch`），因此解析与规范化路径能在无网络环境下被测试。
这是本项目所有适配器的统一形态：IO 边界注入，纯逻辑直测。
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
    classify_error,
    code_in_market,
    normalize_bars,
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


class AkshareAdapter:
    name = "akshare"
    markets = frozenset({"A"})
    #: 分红送配接口不带日期参数，一次给全史，所以确认能做到「截至今天的全部历史」
    corp_actions_full_history = True

    def __init__(
        self,
        fetch: Callable[[str, date, date], pl.DataFrame] = akshare_fetch,
        fhps_fetch: Callable[[str], pl.DataFrame] = akshare_fhps,
    ) -> None:
        self._fetch = fetch
        self._fhps_fetch = fhps_fetch

    def corp_actions(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        """A 股分红送配。源没有日期参数，一次给全史——那就把全史都交回去，**不按窗口切**。

        窗口切片看着只是少存几行，实际是让库与覆盖标记说反话：服务层会写下
        「全历史，确认至今天」，而库里缺着窗口外那些真的除权日。守卫照着覆盖标记放行，
        老日子的区间涨跌就变成裸价——正是 U12 那个 −3.81%。
        """
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
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
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
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



def _as_polars(frame) -> pl.DataFrame:
    if isinstance(frame, pl.DataFrame):
        return frame
    return pl.from_pandas(frame)


# 注册进 datasource.sources：加源只改自己这一行，不动 registry / bootstrap / CLI
source("akshare", note="akshare A 股主源（日线 + 分红送配）")(AkshareAdapter)
