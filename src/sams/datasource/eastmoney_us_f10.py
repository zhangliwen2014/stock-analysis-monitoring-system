"""美股主要财务指标：东财 F10 `RPT_USF10_FN_{G,B,I}MAININDICATOR`（D228）。

与港股同一条路，两处不同：

* **披露日不可信**。源有 `NOTICE_DATE`，但老数据上它是东财的入库日——苹果 2012 年报
  写成 2015-01-28，拿它做 PIT 会把一份十年前的报表说成三年前才公开。所以行按
  `published_date=NULL` 入库，选股按「报告期 ≤ 快照日」取最近一个财年（D215 的同一条口径）。
* **财年不一定 12-31 收尾**。苹果的 2025 年报报告期是 2025-09-27；年报口径认源给的
  `DATE_TYPE=年报`（落库在 `report_type`），不按日历日判（D228）。

一般企业/银行/保险三张报表的列不同：先用 `RPT_USF10_DATA_MAININDICATOR` 查
SECUCODE 与 ORG_TYPE，再取对应那张。银行的营业收入列叫 `TOTAL_INCOME`，
没有毛利/净利率/流动比率——缺的列留 NULL，不拿别的表硬凑（D217）。

只取年报行：同一张表里年报与单季报混排，季度 ROE 是三个月的数，冒充年报会让
「年报 ROE」变成一句假话（D62）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import date

import polars as pl

from sams.core.codes import canonical_code
from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift
from sams.core.fundamentals import PERCENT_DIVISOR
from sams.datasource.base import classify_error
from sams.datasource.f10 import _as_date
from sams.datasource.fundamentals import empty_financials
from sams.datasource.sources import source

US_INDICATOR_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
PROFILE_REPORT = "RPT_USF10_DATA_MAININDICATOR"

logger = logging.getLogger("sams.datasource.eastmoney_us_f10")

#: 代码会拼进过滤器：只放行字母、数字、点、横线（`BRK.B` / `BF-B`）。
#: 引号与空格一律拒——发出去就是一次过滤器注入。
_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]*$")

#: 年报行在源里的标记。`DATE_TYPE` 是中文标签，`DATE_TYPE_CODE` 才是稳定键
_ANNUAL_CODE = "001"
_ANNUAL_LABEL = "年报"

#: 一般企业/银行/保险三张报表：各自的 reportName、columns、列映射与必需列。
#: columns 那一格：一般企业用源预先拼好的视图名，另外两张用 ALL——这是页面的原样。
_REPORTS: dict[str, dict] = {
    "一般企业": {
        "report": "RPT_USF10_FN_GMAININDICATOR",
        "columns": "USF10_FN_GMAININDICATOR",
        "ratios": {
            "roe": "ROE_AVG",
            "revenue_yoy": "OPERATE_INCOME_YOY",
            "profit_yoy": "PARENT_HOLDER_NETPROFIT_YOY",
            "roa": "ROA",
            "gross_margin": "GROSS_PROFIT_RATIO",
            "net_margin": "NET_PROFIT_RATIO",
            "debt_ratio": "DEBT_ASSET_RATIO",
        },
        "plain": {
            "revenue": "OPERATE_INCOME",
            "net_profit": "PARENT_HOLDER_NETPROFIT",
            "eps": "BASIC_EPS",
            "current_ratio": "CURRENT_RATIO",
        },
        "required": ("ROE_AVG", "OPERATE_INCOME_YOY", "PARENT_HOLDER_NETPROFIT_YOY"),
    },
    "银行": {
        "report": "RPT_USF10_FN_BMAININDICATOR",
        "columns": "ALL",
        "ratios": {
            "roe": "ROE",
            "revenue_yoy": "TOTAL_INCOME_YOY",
            "profit_yoy": "PARENT_HOLDER_NETPROFIT_YOY",
            "roa": "ROA",
        },
        "plain": {
            "revenue": "TOTAL_INCOME",
            "net_profit": "PARENT_HOLDER_NETPROFIT",
            "eps": "BASIC_EPS_CS",
        },
        "required": ("ROE", "TOTAL_INCOME_YOY", "PARENT_HOLDER_NETPROFIT_YOY"),
    },
    "保险": {
        "report": "RPT_USF10_FN_IMAININDICATOR",
        "columns": "ALL",
        "ratios": {
            "roe": "ROE",
            "revenue_yoy": "TOTAL_INCOME_YOY",
            "profit_yoy": "PARENT_HOLDER_NETPROFIT_YOY",
            "roa": "ROA",
            "debt_ratio": "DEBT_RATIO",
        },
        "plain": {
            "revenue": "TOTAL_INCOME",
            "net_profit": "PARENT_HOLDER_NETPROFIT",
            "eps": "BASIC_EPS_CS",
        },
        "required": ("ROE", "TOTAL_INCOME_YOY", "PARENT_HOLDER_NETPROFIT_YOY"),
    },
}
_DEFAULT_TYPE = "一般企业"


def report_for(org_type: str) -> dict:
    """按组织类型取报表；不认识的类型落到一般企业（源里绝大多数是它）。"""
    return _REPORTS.get(org_type, _REPORTS[_DEFAULT_TYPE])


def parse_us_financials(
    payload: object,
    *,
    market: str,
    code: str,
    source: str,
    org_type: str = _DEFAULT_TYPE,
    start: date | None = None,
    end: date | None = None,
) -> pl.DataFrame:
    """数据中心 payload → `financials` 标准列；只有年报行，`published_date` 留 NULL。"""
    if not isinstance(payload, dict):
        raise FieldDrift(source, f"美股指标响应不是对象: {type(payload).__name__}")
    if "result" not in payload:
        raise FieldDrift(source, f"{market} {code} 的美股指标响应缺少 result（接口可能改版）")
    result = payload["result"]
    if result is None:
        # 实测查无记录时是 {"result": null, "success": false, "message": "返回数据为空"}
        return empty_financials()
    if not isinstance(result, dict):
        raise FieldDrift(source, f"{market} {code} 的 result 不是对象")
    if "data" not in result:
        raise FieldDrift(source, f"{market} {code} 的美股指标响应缺少 result.data")
    items = result["data"] or []
    if not isinstance(items, list):
        raise FieldDrift(source, f"{market} {code} 的 result.data 不是列表")
    if not items:
        return empty_financials()

    spec = report_for(org_type)
    first = items[0] if isinstance(items[0], dict) else {}
    absent = [
        name
        for name in ("DATE_TYPE_CODE", *spec["required"])
        if name not in first
    ]
    if absent:
        raise FieldDrift(
            source,
            f"{market} {code} 的{org_type}指标缺少必需列 {absent}，实际列 {list(first)}",
        )
    optional = [
        name
        for name in (*spec["ratios"].values(), *spec["plain"].values())
        if name not in first
    ]
    if optional:
        # 展示列整列消失不拖垮 ROE 这条链，但必须出声（D217）
        logger.warning(
            "%s %s 的%s指标缺少列 %s，按 NULL 入库", source, code, org_type, optional
        )

    blank = {name: None for name in empty_financials().columns}
    canonical = canonical_code(market, code)
    rows = []
    dropped = 0
    for item in items:
        if not isinstance(item, dict):
            dropped += 1
            continue
        echoed = item.get("SECURITY_CODE")
        if echoed and canonical_code(market, str(echoed)) != canonical:
            raise FieldDrift(
                source,
                f"美股指标响应代码 {echoed} 与请求代码 {code} 不一致（串包或缓存错位）",
            )
        if str(item.get("DATE_TYPE_CODE")) != _ANNUAL_CODE:
            continue
        report_date = _as_date(item.get("REPORT_DATE"))
        if report_date is None:
            dropped += 1
            continue
        rows.append(
            {
                **blank,
                "market": market,
                "code": canonical,
                "report_date": report_date,
                # 源的 NOTICE_DATE 是老数据的入库日，不是 10-K 的公开日：留 NULL（D228）
                "published_date": None,
                "report_type": _ANNUAL_LABEL,
                # 源的主指标没有分红与市值：这两格留 NULL，不拿按日估值那一行冒充报告期
                "dividend_yield": None,
                "market_cap": None,
                **{
                    target: _number(item.get(name))
                    for target, name in spec["plain"].items()
                },
                **{
                    target: _ratio(item.get(name))
                    for target, name in spec["ratios"].items()
                },
                "source": source,
            }
        )

    if dropped:
        logger.warning("%s %s 丢弃 %d 行美股指标（报告期解析不出）", source, code, dropped)
    if not rows:
        raise EmptyResponse(source, f"{market} {code} 的美股指标没有一行年报能解析出报告期")

    out = pl.DataFrame(rows, schema=dict(empty_financials().schema))
    if start is not None:
        out = out.filter(pl.col("report_date") >= start)
    if end is not None:
        out = out.filter(pl.col("report_date") <= end)
    return out.unique(subset=["report_date"], keep="last").sort("report_date")


def _ratio(value: object) -> float | None:
    try:
        return float(value) / PERCENT_DIVISOR  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _number(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _checked_code(code: str, source: str) -> str:
    if not _CODE_PATTERN.match(code):
        raise DatasourceError(
            source, f"美股代码 {code!r} 含有过滤器不接受的字符，已拒绝请求"
        )
    return code


def identify_us(
    get: Callable[[str, dict[str, str]], dict], *, code: str, source: str
) -> tuple[str | None, str]:
    """SECURITY_CODE → (SECUCODE, 组织类型)。查无记录时返回 (None, "")。"""
    payload = get(
        US_INDICATOR_URL,
        {
            "reportName": PROFILE_REPORT,
            "columns": "SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,ORG_TYPE",
            "filter": f'(SECURITY_CODE="{_checked_code(code, source)}")',
            "pageNumber": "1",
            "pageSize": "10",
        },
    )
    if not isinstance(payload, dict) or "result" not in payload:
        raise FieldDrift(
            source, f"美股证券概览响应缺少 result（拿到 {type(payload).__name__}）"
        )
    result = payload["result"]
    if result is None:
        return None, ""
    if not isinstance(result, dict):
        raise FieldDrift(source, "美股证券概览的 result 不是对象")
    data = result.get("data") or []
    if not isinstance(data, list):
        raise FieldDrift(source, "美股证券概览的 result.data 不是列表")
    if not data:
        return None, ""
    row = data[0] if isinstance(data[0], dict) else {}
    secucode = str(row.get("SECUCODE") or "").strip()
    if not secucode:
        raise FieldDrift(source, f"美股证券概览里没有 SECUCODE（拿到 {row}）")
    if not _CODE_PATTERN.match(secucode):
        raise FieldDrift(source, f"美股 SECUCODE {secucode!r} 含意外字符，已拒绝")
    return secucode, str(row.get("ORG_TYPE") or "")


def fetch_us_financials(
    get: Callable[[str, dict[str, str]], dict],
    *,
    market: str,
    code: str,
    start: date,
    end: date,
    source: str,
) -> pl.DataFrame:
    """一只美股的年报行：先认组织类型，再取对应的那张表。查无此票是 0 行。"""
    secucode, org_type = identify_us(get, code=code, source=source)
    if secucode is None:
        # 源不认识这只票：一句「查无记录」，不是故障（D43）
        return empty_financials()
    spec = report_for(org_type)
    payload = get(
        US_INDICATOR_URL,
        {
            "reportName": spec["report"],
            "columns": spec["columns"],
            "filter": f'(SECUCODE="{secucode}")(DATE_TYPE_CODE in ("{_ANNUAL_CODE}"))',
            "pageNumber": "1",
            "pageSize": "200",
            "sortTypes": "-1",
            "sortColumns": "REPORT_DATE",
            "source": "SECURITIES",
            "client": "PC",
        },
    )
    return parse_us_financials(
        payload,
        market=market,
        code=code,
        source=source,
        org_type=org_type,
        start=start,
        end=end,
    )


def _http_get(url: str, params: dict[str, str]) -> dict:
    # 同 eastmoney_f10：模块级 import eastmoney 会撞上 sources 的导入期 discover
    from sams.datasource.eastmoney import http_get

    return http_get(url, params)


class EastmoneyUSF10Adapter:
    name = "eastmoney_us_f10"
    markets = frozenset({"US"})

    def __init__(self, get: Callable[[str, dict[str, str]], dict] | None = None) -> None:
        self._get = get or _http_get

    def financials(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        try:
            return fetch_us_financials(
                self._get,
                market=market,
                code=code,
                start=start,
                end=end,
                source=self.name,
            )
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc


source("eastmoney_us_f10", note="东方财富美股主要财务指标（仅 US）")(EastmoneyUSF10Adapter)
