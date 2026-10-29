"""港股主要财务指标：东财数据中心 `RPT_HKF10_FN_MAININDICATOR`。

源给 ROE 与营收/利润同比，但**不给披露日**。这条缺口决定了这些行的用途（D215）：
照常入 `financials`，`published_date` 留 NULL；`load_as_of` / `history` 认的是
`published_date <= as_of`，所以它们不进入 PR、选股与「已披露」判断，只供个股页看数。
拿报告期加天数去猜披露日，等于往回开时间机器（D61）。
"""

from __future__ import annotations

import logging
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

HK_INDICATOR_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
REPORT_NAME = "RPT_HKF10_FN_MAININDICATOR"

logger = logging.getLogger("sams.datasource.eastmoney_hk_f10")

#: 源给的三个比率都是百分数（9.96 = 9.96%），÷100 只在这一处发生（D58）
_RATIOS = {
    "ROE_AVG": "roe",
    "OPERATE_INCOME_YOY": "revenue_yoy",
    "HOLDER_PROFIT_YOY": "profit_yoy",
}


def parse_hk_financials(
    payload: object,
    *,
    market: str,
    code: str,
    source: str,
    start: date | None = None,
    end: date | None = None,
) -> pl.DataFrame:
    """数据中心 payload → `financials` 标准列；`result: null` 是「没有」的 0 行答复。"""
    if not isinstance(payload, dict):
        raise FieldDrift(source, f"港股指标响应不是对象: {type(payload).__name__}")
    if "result" not in payload:
        raise FieldDrift(source, f"{market} {code} 的港股指标响应缺少 result（接口可能改版）")
    result = payload["result"]
    if result is None:
        # 实测查无记录时是 {"result": null, "success": false, "message": "返回数据为空"}
        return empty_financials()
    if not isinstance(result, dict):
        raise FieldDrift(source, f"{market} {code} 的 result 不是对象")
    if "data" not in result:
        raise FieldDrift(source, f"{market} {code} 的港股指标响应缺少 result.data")
    items = result["data"] or []
    if not isinstance(items, list):
        raise FieldDrift(source, f"{market} {code} 的 result.data 不是列表")
    if not items:
        return empty_financials()

    first = items[0] if isinstance(items[0], dict) else {}
    absent = [name for name in ("REPORT_DATE", *_RATIOS) if name not in first]
    if absent:
        raise FieldDrift(
            source,
            f"{market} {code} 的港股指标缺少必需列 {absent}，实际列 {list(first)}",
        )

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
                f"港股指标响应代码 {echoed} 与请求代码 {code} 不一致（串包或缓存错位）",
            )
        report_date = _as_date(item.get("REPORT_DATE"))
        if report_date is None:
            dropped += 1
            continue
        rows.append(
            {
                "market": market,
                "code": canonical,
                "report_date": report_date,
                # 源不给披露日：留 NULL，不拿 REPORT_DATE + 45 天去猜（D61/D215）
                "published_date": None,
                "roe": _ratio(item.get("ROE_AVG")),
                "revenue_yoy": _ratio(item.get("OPERATE_INCOME_YOY")),
                "profit_yoy": _ratio(item.get("HOLDER_PROFIT_YOY")),
                # 源的 DIVIDEND_RATE / TOTAL_MARKET_CAP 时点与口径未核实，不往里塞
                "dividend_yield": None,
                "market_cap": None,
                "source": source,
            }
        )

    if dropped:
        logger.warning("%s %s 丢弃 %d 行港股指标（报告期解析不出）", source, code, dropped)
    if not rows:
        raise EmptyResponse(source, f"{market} {code} 的港股指标没有一行能解析出报告期")

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


def _http_get(url: str, params: dict[str, str]) -> dict:
    # 同 eastmoney_f10：模块级 import eastmoney 会撞上 sources 的导入期 discover
    from sams.datasource.eastmoney import http_get

    return http_get(url, params)


class EastmoneyHKF10Adapter:
    name = "eastmoney_hk_f10"
    markets = frozenset({"HK"})

    def __init__(self, get: Callable[[str, dict[str, str]], dict] | None = None) -> None:
        self._get = get or _http_get

    def financials(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        params = {
            "reportName": REPORT_NAME,
            "columns": "ALL",
            "filter": f'(SECUCODE="{canonical_code(market, code)}.HK")',
            "sortColumns": "REPORT_DATE",
            "sortTypes": "-1",
            "pageSize": "100",
        }
        try:
            payload = self._get(HK_INDICATOR_URL, params)
            return parse_hk_financials(
                payload, market=market, code=code, source=self.name, start=start, end=end
            )
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc


source("eastmoney_hk_f10", note="东方财富港股主要财务指标（仅 HK）")(EastmoneyHKF10Adapter)
