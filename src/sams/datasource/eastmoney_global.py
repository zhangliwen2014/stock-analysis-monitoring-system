"""东财港美股：分钟 K、HK 指数日线、HK 上市日期、HK/US 基本面、全市场快照（D324）。

这一路补的是港美股的「链上第二个源」，不是新数据族。几条实测事实决定了每个分支：

* **分钟 K 的时间是北京时间**（`2026-11-05 15:55` 这种）。港股与北京同区，原样落；
  美股要折回纽约——北京 01:50 是纽约**前一天** 12:50（冬令时），不折的话一根盘中 K 会跨到第二
  天，图上多出一整段假夜盘。`clock` 是这一根的结束时刻，与腾讯分钟 K 同口径。
  请求窗口也跟着这一点走：`beg/end` 按北京日期过滤，纽约 D 的盘落在北京 [D, D+1]，
  只问 [D, D] 实测回 0 根，所以 US 的请求窗口放宽一天、出帧再裁回纽约日期。
* **港股的 secid 是 `116.<五位代码>`，美股要探 `105/106/107`**。东财把类股写成下划线
  （`106.BRK_B`），点号形态（`106.BRK.B`）回 `data: null`——探不到时是一句「查无记录」，
  不能把「问错了写法」说成「这只票没有分钟 K」。
* **HK 指数日线的 secid 不统一**：恒指与国企指数在 100，恒生科技在 124；指数字段第三格
  叫 `latest`，它就是收盘价。
* **快照是全表分页**：`clist` 忽略 `pz=5000`，一页硬上限 100；港股 4745 行（48 页）、
  美股 13899 行（139 页），实测 HK 36–60s、US 99–167s（两次；网络与东财限流会变）。
  所以它挂在快照链的最后（D325），而 `note` 里写的是实测秒数，不是「较慢」。
* **同一份基本面接口**：HK 用 F10 主要指标全史，US 与 `eastmoney_us_f10` 同一个
  数据中心的报表——按 D322 第 2 档，`note` 里必须写明「同源」。

US 分钟 K 与 HK 指数日线的请求都按 `[start, end]` 传 `beg/end`：接口对这两个参数
是过滤而不是截断，历史很短时也不会漏根。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from sams.core.codes import canonical_code, canonical_market, looks_like_index_code
from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift
from sams.core.quotes import MARKET_ZONES
from sams.datasource.base import classify_error, normalize_bars
from sams.datasource.eastmoney_hk_f10 import parse_hk_financials
from sams.datasource.eastmoney_us_f10 import fetch_us_financials
from sams.datasource.f10 import _as_date
from sams.datasource.minute import empty_minute_bars
from sams.datasource.sina_global import sina_us_ticker
from sams.datasource.sources import source
from sams.datasource.spot import SPOT_SCHEMA

logger = logging.getLogger("sams.datasource.eastmoney_global")

SOURCE = "eastmoney_global"

#: 东财的时间列一律北京时间（与 A 股同一台机房）
SHANGHAI = ZoneInfo("Asia/Shanghai")

_MINUTE_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S")


def _payload_data(payload: object, *, what: str) -> dict:
    """`data` 在不在决定「改版」还是「没有」：前者要出声，后者是一句答复。"""
    if not isinstance(payload, dict):
        raise FieldDrift(SOURCE, f"{what}响应不是对象: {type(payload).__name__}")
    if "data" not in payload:
        raise FieldDrift(SOURCE, f"{what}响应缺少 data（接口可能改版）")
    data = payload["data"]
    if data is None:
        raise EmptyResponse(SOURCE, f"{what}没有记录")
    if not isinstance(data, dict):
        raise FieldDrift(SOURCE, f"{what}响应的 data 不是对象: {type(data).__name__}")
    return data


def _klines(data: dict, *, what: str) -> list:
    lines = data.get("klines")
    if lines is None:
        raise FieldDrift(SOURCE, f"{what}响应缺少 data.klines")
    if not isinstance(lines, list):
        raise FieldDrift(SOURCE, f"{what}的 klines 不是列表")
    if not lines:
        raise EmptyResponse(SOURCE, f"{what}没有记录")
    return lines


def _digits(parts: list[str], indexes: tuple[int, ...]) -> list[float] | None:
    try:
        return [float(parts[index]) for index in indexes]
    except (TypeError, ValueError):
        return None


def parse_em_minute(
    payload: object,
    *,
    market: str,
    code: str,
    start: date,
    end: date,
    zone: ZoneInfo | None = None,
) -> pl.DataFrame:
    """kline 帧 → `MINUTE_BARS_COLUMNS`；时间从北京折回市场时区。

    整批时刻都解析不出时是 `FieldDrift`（源换了时间格式），窗口内一根都没有时才是
    `EmptyResponse`——前者不能换源了事，后者可以。
    """
    market = canonical_market(market)
    zone = zone or MARKET_ZONES[market]
    what = f"{market} {code} 的分钟 K"
    lines = _klines(_payload_data(payload, what=what), what=what)
    # 指数码原样保留（`hkHSI` 过一遍归一会折成个股）；个股才归一
    label = code.strip() if looks_like_index_code(market, code) else canonical_code(market, code)

    rows: list[dict[str, Any]] = []
    dropped = 0
    for line in lines:
        parts = str(line).split(",")
        if len(parts) < 7:
            dropped += 1
            continue
        moment = None
        for fmt in _MINUTE_FORMATS:
            try:
                moment = datetime.strptime(parts[0].strip(), fmt)
                break
            except ValueError:
                continue
        if moment is None:
            dropped += 1
            continue
        values = _digits(parts, (1, 2, 3, 4, 5, 6))
        if values is None or values[1] <= 0:
            dropped += 1
            continue
        open_, close, high, low, volume, amount = values
        local = moment.replace(tzinfo=SHANGHAI).astimezone(zone)
        rows.append(
            {
                "market": market,
                "code": label,
                "date": local.date(),
                "clock": local.strftime("%H:%M"),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": amount,
                "source": SOURCE,
            }
        )

    if dropped:
        logger.warning("%s %s 丢弃 %d 根（时刻或价格解析不出）", SOURCE, label, dropped)
    if not rows:
        raise FieldDrift(SOURCE, f"{what}没有一行能解析出时刻与价格")

    out = pl.DataFrame(rows, schema=dict(empty_minute_bars().schema))
    out = out.filter(pl.col("date").is_between(start, end))
    if out.is_empty():
        raise EmptyResponse(SOURCE, f"{what}在 {start}~{end} 内 0 根")
    return out.unique(subset=["date", "clock"], keep="last").sort(["date", "clock"])


# ---------- 实网取数与 secid 形态 ----------

KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
STOCK_URL = "https://push2.eastmoney.com/api/qt/stock/get"

#: 分钟档只认协议里那六档。其它 `klt` 在接口上是日/周/月，静默当成分钟会画出一张假图
EM_MINUTE_SCALES = frozenset({1, 5, 15, 30, 60, 120})

#: 美股 kline 的 secid 前缀：105 纳斯达克、106 纽交所、107 美交所。探不到就是查无记录
US_SECID_MARKETS = ("105", "106", "107")


def em_us_symbol(code: str) -> str:
    """本系统美股代码 → 东财 kline 的符号：剥交易所后缀，类股的点写成下划线。

    `BRK.B` → `BRK_B`（接口对点号形态回 `data: null`），`AAPL.OQ` → `AAPL`。
    后缀那一条与新浪日线共用 `sina_us_ticker`，不在这里再抄一份。
    """
    return sina_us_ticker(code).replace(".", "_")


def fetch_kline(secid: str, klt: int, start: date, end: date) -> dict:
    """真实网络调用：一根 K 线序列（分钟或日线，不复权）。"""
    return _http_get(
        KLINE_URL,
        {
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
            "klt": str(klt),
            "fqt": "0",
            "beg": start.strftime("%Y%m%d"),
            "end": end.strftime("%Y%m%d"),
        },
    )


def resolve_us_secid(symbol: str) -> str | None:
    """探 `105/106/107`：回显的 `f57` 与请求符号相等才算问到，全部问不到返回 None。"""
    for market in US_SECID_MARKETS:
        secid = f"{market}.{symbol}"
        payload = _http_get(STOCK_URL, {"secid": secid, "fields": "f57,f58"})
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict) and str(data.get("f57") or "").upper() == symbol.upper():
            return secid
    return None


def _http_get(url: str, params: dict[str, str]) -> dict:
    # 同 eastmoney_f10：模块级 import eastmoney 会撞上 sources 的导入期 discover
    from sams.datasource.eastmoney import http_get

    return http_get(url, params)


class EastmoneyGlobalAdapter:
    name = SOURCE
    markets = frozenset({"HK", "US"})
    minute_bars_markets = frozenset({"HK", "US"})
    market_snapshot_markets = frozenset({"HK", "US"})

    def __init__(
        self,
        *,
        kline_fetch: Callable[[str, int, date, date], dict] = fetch_kline,
        us_secid_fetch: Callable[[str], str | None] = resolve_us_secid,
        get: Callable[[str, dict[str, str]], dict] | None = None,
        spot_fetch: Callable[[str], list[dict]] | None = None,
    ) -> None:
        self._kline = kline_fetch
        self._us_secid = us_secid_fetch
        self._get = get or _http_get
        self._spot = spot_fetch or fetch_snapshot
        self._secids: dict[str, str] = {}

    def _secid_for(self, market: str, code: str) -> str:
        if market == "HK":
            return f"116.{canonical_code('HK', code)}"
        symbol = em_us_symbol(code)
        cached = self._secids.get(symbol)
        if cached is not None:
            return cached
        secid = self._us_secid(symbol)
        if secid is None:
            # 三个前缀都问不到：这是「源查无此票」的答复，不是故障
            raise EmptyResponse(SOURCE, f"US {code} 在 105/106/107 都查不到")
        self._secids[symbol] = secid
        return secid

    def listed_date(self, market: str, code: str) -> date | None:
        """港股上市首日。美股这一路没有上市日期：返回 None，不替它编一个成立日。"""
        if canonical_market(market) != "HK":
            return None
        try:
            payload = self._get(
                DATA_URL,
                {
                    "reportName": HK_PROFILE_REPORT,
                    "columns": "SECUCODE,SECURITY_CODE,LISTING_DATE",
                    "quoteColumns": "",
                    "filter": f'(SECUCODE="{canonical_code("HK", code)}.HK")',
                    "pageNumber": "1",
                    "pageSize": "10",
                    "sortTypes": "",
                    "sortColumns": "",
                    "source": "F10",
                    "client": "PC",
                },
            )
            return parse_hk_listed_date(payload)
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc

    def index_daily(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        """港股指数日线。美股指数这一路没有：`EmptyResponse` 让链继续问下一个源。"""
        market = canonical_market(market)
        if market != "HK":
            raise EmptyResponse(self.name, f"东财港美股指数日线不供 {market}")
        if end < start:
            raise ValueError(f"区间反向: {start} > {end}")
        secid = HK_INDEX_SECIDS.get(code.strip())
        if secid is None:
            raise EmptyResponse(self.name, f"东财不认识港股指数代码 {code}")
        try:
            payload = self._kline(secid, 101, start, end)
            return parse_em_hk_index(payload, code=code.strip(), start=start, end=end)
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc

    def minute_bars(
        self, market: str, code: str, start: date, end: date, *, scale: int, count: int
    ) -> pl.DataFrame:
        market = canonical_market(market)
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        if market not in self.minute_bars_markets:
            raise EmptyResponse(self.name, f"东财港美股分钟K不供 {market}")
        if end < start:
            raise ValueError(f"区间反向: {start} > {end}")
        if scale not in EM_MINUTE_SCALES:
            raise ValueError(f"东财分钟K只认 {sorted(EM_MINUTE_SCALES)}，收到 {scale}")
        if looks_like_index_code(market, code):
            raise EmptyResponse(self.name, f"东财分钟K这一路不供指数 {code}")
        try:
            secid = self._secid_for(market, code)
            begin, finish = start, end
            if market == "US":
                # `beg/end` 按北京日期过滤（实测）：纽约 D 的盘落在北京 [D, D+1]，
                # 只问 [D, D] 回 0 根。放宽一天，出帧再按纽约日期裁回窗口。
                begin, finish = start - timedelta(days=1), end + timedelta(days=1)
            payload = self._kline(secid, scale, begin, finish)
            return parse_em_minute(
                payload, market=market, code=code, start=start, end=end
            )
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc

    def financials(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        """HK 复用港股 F10 的主要指标全史；US 复用美股 F10 的年报行（同源备份，D322）。"""
        market = canonical_market(market)
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        if end < start:
            raise ValueError(f"区间反向: {start} > {end}")
        try:
            if market == "US":
                return fetch_us_financials(
                    self._get,
                    market=market,
                    code=code,
                    start=start,
                    end=end,
                    source=self.name,
                )
            payload = self._get(
                DATA_URL,
                {
                    "reportName": HK_FIN_REPORT,
                    "columns": "ALL",
                    "quoteColumns": "",
                    "filter": f'(SECUCODE="{canonical_code("HK", code)}.HK")',
                    "pageNumber": "1",
                    "pageSize": "100",
                    "sortTypes": "-1",
                    "sortColumns": "REPORT_DATE",
                    "source": "F10",
                    "client": "PC",
                },
            )
            return parse_hk_financials(
                payload,
                market=market,
                code=code,
                source=self.name,
                start=start,
                end=end,
            )
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc

    def market_snapshot(self, market: str, codes: list[str]) -> pl.DataFrame:
        """一整个市场的横截面：整表分页取回后再筛请求的代码。

        `clist` 不按代码问，所以这里没法省掉 48/139 页——也就没法在链上排到
        腾讯前面（D325）。一次调用取一次表，不跨调用缓存：两次调用之间全是交易时间。
        """
        market = canonical_market(market)
        if market not in self.market_snapshot_markets:
            raise DatasourceError(
                self.name,
                f"东财港美股快照只供 {sorted(self.market_snapshot_markets)}，收到 {market}",
            )
        wanted = list(dict.fromkeys(canonical_code(market, code) for code in codes))
        if not wanted:
            raise EmptyResponse(self.name, "快照请求里没有代码")
        try:
            return parse_em_spot(self._spot(market), market=market, codes=wanted)
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc


source(
    "eastmoney_global",
    note=(
        "东方财富港美股（分钟K、HK指数日线、HK上市日期、HK/US基本面与快照；"
        "基本面与 eastmoney_hk_f10 / eastmoney_us_f10 同源；"
        "快照整表分页，实测 HK 约 35–60s / US 约 100–170s）"
    ),
)(EastmoneyGlobalAdapter)


# ---------- HK 指数日线 ----------

#: 系统里的三个港股指数 → secid。恒指/国企在 100，恒生科技在 124（实测 2026-11-05）；
#: 不认识的指数码是一句答复，不去猜第二个前缀再问一次。
HK_INDEX_SECIDS = {
    "hkHSI": "100.HSI",
    "hkHSTECH": "124.HSTECH",
    "hkHSCEI": "100.HSCEI",
}

_INDEX_COLUMNS = {
    "date": "date",
    "open": "open",
    "close": "close",
    "high": "high",
    "low": "low",
    "volume": "volume",
    "amount": "amount",
}


def parse_em_hk_index(
    payload: object, *, code: str, start: date, end: date
) -> pl.DataFrame:
    """指数 kline 帧 → `BAR_COLUMNS`。第三格是 `latest`，接口用它装收盘价。"""
    what = f"HK 指数 {code} 的日线"
    lines = _klines(_payload_data(payload, what=what), what=what)
    rows = []
    for line in lines:
        parts = str(line).split(",")
        if len(parts) < 7:
            continue
        values = _digits(parts, (1, 2, 3, 4, 5, 6))
        if values is None:
            continue
        open_, close, high, low, volume, amount = values
        rows.append(
            {
                "date": parts[0].strip(),
                "open": open_,
                "close": close,
                "high": high,
                "low": low,
                "volume": volume,
                "amount": amount,
            }
        )
    if not rows:
        raise FieldDrift(SOURCE, f"{what}没有一行能解析出日期与价格")

    out = normalize_bars(
        pl.DataFrame(rows),
        market="HK",
        code=code,
        source=SOURCE,
        column_map=_INDEX_COLUMNS,
    )
    # 指数码是源形态（hkHSI），`normalize_bars` 会把它折成个股写法（D145）
    out = out.with_columns(pl.lit(code).alias("code"))
    out = out.filter(pl.col("date").is_between(start, end))
    if out.is_empty():
        raise EmptyResponse(SOURCE, f"{what}在 {start}~{end} 内没有交易")
    return out


# ---------- HK 上市日期 ----------

DATA_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
HK_PROFILE_REPORT = "RPT_HKF10_INFO_ORGPROFILE"
HK_FIN_REPORT = "RPT_HKF10_FN_MAININDICATOR"


def parse_hk_listed_date(payload: object) -> date | None:
    """公司概况 → 上市首日。`LISTING_DATE` 缺列是改版，值为空是「源没有这一格」。"""
    if not isinstance(payload, dict):
        raise FieldDrift(SOURCE, f"港股概况响应不是对象: {type(payload).__name__}")
    if "result" not in payload:
        raise FieldDrift(SOURCE, "港股概况响应缺少 result（接口可能改版）")
    result = payload["result"]
    if result is None:
        # 实测查无记录时是 {"result": null, "success": false, "message": "返回数据为空"}
        return None
    if not isinstance(result, dict):
        raise FieldDrift(SOURCE, "港股概况的 result 不是对象")
    if "data" not in result:
        raise FieldDrift(SOURCE, "港股概况响应缺少 result.data")
    items = result["data"] or []
    if not isinstance(items, list):
        raise FieldDrift(SOURCE, "港股概况的 result.data 不是列表")
    if not items:
        return None
    first = items[0] if isinstance(items[0], dict) else {}
    if "LISTING_DATE" not in first:
        raise FieldDrift(SOURCE, f"港股概况缺少 LISTING_DATE，实际列 {list(first)}")
    return _as_date(first.get("LISTING_DATE"))


# ---------- 全市场快照 ----------

CLIST_URL = "https://72.push2.eastmoney.com/api/qt/clist/get"

#: 港股行情段：主板/创业板/ETF/指数分开列，`m:116` 会混进 17465 行牛熊证。
#: 美股三个前缀一页全要（实测 4745 / 13899 行）。
CLIST_FS = {
    "HK": "m:128 t:3,m:128 t:4,m:128 t:1,m:128 t:2",
    "US": "m:105,m:106,m:107",
}

#: `pz` 的上限就是 100：传 5000 仍回 100 行，分页是唯一取全表的办法
SPOT_PAGE_SIZE = 100

#: 页数护栏：正常是 48 / 139 页；走到这里说明 total 与实际不符，宁可报改版
MAX_SPOT_PAGES = 300

_SPOT_FIELDS = "f2,f3,f5,f6,f7,f8,f9,f10,f12,f13,f14,f20,f21,f23,f115,f124"

#: 工作帧列 ← clist 字段。f2 现价、f3 涨跌幅、f6 成交额、f7 振幅、f8 换手率、
#: f9 PE(动)、f10 量比、f115 PE(TTM)、f20/f21 总/流通市值、f23 PB、f124 UTC 秒
_SPOT_INDEX = {
    "name": "f14",
    "price": "f2",
    "pct_chg": "f3",
    "amount": "f6",
    "turnover_rate": "f8",
    "volume_ratio": "f10",
    "amplitude": "f7",
    "pe_ttm": "f115",
    "pe_dyn": "f9",
    "pb": "f23",
    "market_cap": "f20",
    "float_cap": "f21",
}


def _spot_number(item: dict, field: str) -> float | None:
    """`"-"` 与空串是「源不知道这一格」；解析不出也给 None，不编 0。"""
    try:
        return float(item.get(field))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _spot_day(value: object, zone: ZoneInfo) -> date | None:
    try:
        moment = datetime.fromtimestamp(float(value), UTC)  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return moment.astimezone(zone).date()


def parse_em_spot(
    rows: list, *, market: str, codes: list[str], zone: ZoneInfo | None = None
) -> pl.DataFrame:
    """clist 整表行 → `SPOT_SCHEMA`，只留请求的代码。一行都没对上时是一句答复。"""
    market = canonical_market(market)
    zone = zone or MARKET_ZONES[market]
    wanted = {canonical_code(market, code) for code in codes}

    frame_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("f12") or "").strip()
        if not raw:
            continue
        # 美股类股在源里写 `BRK_B`（点号形态查不到），回到本系统的 `BRK.B`
        code = canonical_code(market, raw.replace("_", ".") if market == "US" else raw)
        if code not in wanted or code in seen:
            continue
        seen.add(code)
        name = str(item.get(_SPOT_INDEX["name"]) or "").strip()
        frame_rows.append(
            {
                "market": market,
                "code": code,
                "name": name or None,
                "quoted_on": _spot_day(item.get("f124"), zone),
                **{
                    target: _spot_number(item, field)
                    for target, field in _SPOT_INDEX.items()
                    if target != "name"
                },
                # 港股与美股都没有 ST 标记（那是 A 股的制度）
                "is_st": False,
                "source": SOURCE,
            }
        )

    if not frame_rows:
        raise EmptyResponse(SOURCE, f"{market} 快照里一行都没对上请求的代码")
    return pl.DataFrame(frame_rows, schema=dict(SPOT_SCHEMA))


def fetch_snapshot(
    market: str, *, get: Callable[[str, dict[str, str]], dict] | None = None
) -> list[dict]:
    """真实网络调用：一个市场的整表。页间停 0.5s，实测 HK 约 35–60s、US 约 100–170s。"""
    market = canonical_market(market)
    fetch = get or _http_get
    try:
        fs = CLIST_FS[market]
    except KeyError:
        raise ValueError(f"东财快照只供 {sorted(CLIST_FS)}，收到 {market}") from None

    rows: list[dict] = []
    for page in range(1, MAX_SPOT_PAGES + 1):
        payload = fetch(
            CLIST_URL,
            {
                "fs": fs,
                "fields": _SPOT_FIELDS,
                "pn": str(page),
                "pz": str(SPOT_PAGE_SIZE),
                "po": "1",
                "np": "1",
                "fltt": "2",
                "invt": "2",
            },
        )
        data = _payload_data(payload, what=f"{market} 快照")
        diff = data.get("diff")
        if diff is None:
            raise FieldDrift(SOURCE, f"{market} 快照响应缺少 data.diff")
        if isinstance(diff, dict):
            # 少数镜像把 diff 给成以序号为键的对象；两种形态都是同一批行
            diff = list(diff.values())
        if not isinstance(diff, list):
            raise FieldDrift(SOURCE, f"{market} 快照的 data.diff 不是列表")
        rows.extend(item for item in diff if isinstance(item, dict))
        total = data.get("total")
        if not diff:
            break
        if isinstance(total, int) and len(rows) >= total:
            break
        time.sleep(0.5)
    else:
        raise FieldDrift(
            SOURCE,
            f"{market} 快照取到 {MAX_SPOT_PAGES} 页仍未结束（total 与实际行数不符）",
        )

    if not rows:
        raise EmptyResponse(SOURCE, f"{market} 快照整表 0 行")
    return rows
