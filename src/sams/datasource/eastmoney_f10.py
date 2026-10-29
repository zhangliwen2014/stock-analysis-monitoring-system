"""东方财富 F10：公司概况与公告（A 股 + 港股）。

两家市场走同一族接口，只差代码形态与 `ann_type`；字段映射放在 `datasource/f10.py`
的纯函数里，这里只管端点、参数与异常归类。公告只取接口的第一页（最多 100 条）：
公告不是任何计算或回测的输入（D216），缺的是更早的条目，不是某个数。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import polars as pl

from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import DatasourceError
from sams.datasource.base import classify_error
from sams.datasource.f10 import (
    empty_profile,
    parse_announcements,
    parse_profile,
)
from sams.datasource.sources import source

A_PROFILE_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax"
HK_PROFILE_URL = "https://emweb.securities.eastmoney.com/PC_HKF10/CompanyProfile/PageAjax"
ANNOUNCEMENTS_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"

#: 北交所号段（与 `core.codes.daily_price_limit` 同一份判据）：这些前缀的 F10 代码
#: 不是 SH/SZ 出身，拼错交易所会让接口回「股票代码不合法」，而屏幕上只会说没有概况
_BJ_PREFIXES = ("43", "83", "87", "88", "92")
_SH_FIRST = "569"


def f10_code(market: str, code: str) -> str:
    """F10 接口认的代码形态：A 股带交易所前缀（`SH600519`），港股是五位数字。"""
    market = canonical_market(market)
    canonical = canonical_code(market, code)
    if market == "A":
        if not canonical.isdigit() or len(canonical) != 6:
            raise ValueError(f"A 股代码应为 6 位数字，收到 {code!r}")
        if canonical.startswith(_BJ_PREFIXES):
            return f"BJ{canonical}"
        return f"{'SH' if canonical[0] in _SH_FIRST else 'SZ'}{canonical}"
    if market == "HK":
        return canonical
    raise ValueError(f"{market} 没有 F10 概况")


def _http_get(url: str, params: dict[str, str]) -> dict:
    # 延迟 import：eastmoney 也要从 `sources` 拿 `source`，而 `sources` 在导入阶段
    # 就 discover 本模块——模块级 import 会撞上「eastmoney 还没执行到 http_get 定义」
    from sams.datasource.eastmoney import http_get

    return http_get(url, params)


class EastmoneyF10Adapter:
    name = "eastmoney_f10"
    markets = frozenset({"A", "HK"})

    def __init__(self, get: Callable[[str, dict[str, str]], dict] | None = None) -> None:
        self._get = get or _http_get

    def company_profile(
        self, market: str, code: str, start: date, end: date
    ) -> pl.DataFrame:
        """概况没有时间窗口；`start`/`end` 只为与降级链同形。"""
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        url = A_PROFILE_URL if market == "A" else HK_PROFILE_URL
        try:
            payload = self._get(url, {"code": f10_code(market, code)})
            return parse_profile(payload, market=market, code=code, source=self.name)
        except DatasourceError:
            raise  # 已经是本域异常，别二次归类
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc

    def announcements(
        self, market: str, code: str, start: date, end: date
    ) -> pl.DataFrame:
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        params = {
            "ann_type": "A" if market == "A" else "H",
            "client_source": "web",
            "page_index": "1",
            "page_size": "100",
            "stock_list": canonical_code(market, code),
            "begin_time": start.isoformat(),
            "end_time": end.isoformat(),
        }
        try:
            payload = self._get(ANNOUNCEMENTS_URL, params)
            return parse_announcements(payload, market=market, code=code, source=self.name)
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc


__all__ = [
    "EastmoneyF10Adapter",
    "empty_profile",
    "f10_code",
    "parse_announcements",
    "parse_profile",
]

source("eastmoney_f10", note="东方财富 F10 概况与公告（A/HK）")(EastmoneyF10Adapter)
