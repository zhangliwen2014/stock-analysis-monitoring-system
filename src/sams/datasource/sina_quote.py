"""新浪 `hq.sinajs.cn`：只提供报价的备源（D114）。

只报 `quote` 能力、不报日线：报价的字段与新鲜度语义和日线完全不同，把两者塞进
一个适配器会让「日线不支持这个市场」与「报价暂时没有」共用一个健康格。

三个市场的字段表不是同一张：
  * A 股：0 名、1 开、2 昨收、3 现价、4 高、5 低、8 量（股）、9 额、
    11 买一、21 卖一、30 日期、31 时间；
  * 港股：0 英文名、1 中文名、2 开、3 昨收、4 高、5 低、6 价、8 涨跌幅、
    9 买一、10 卖一、11 额、12 量、17 日期、18 时间；
  * 美股：0 名、1 价、2 涨跌幅、3 时刻、5 开、6 高、7 低、10 量、26 昨收、30 额。

按「看起来差不多」复用一张表，会把港股的昨收读成现价——这个错误不会抛异常。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_code
from sams.core.errors import DatasourceError, EmptyResponse, RateLimited
from sams.core.quotes import MARKET_ZONES, Quote, QuoteBatch
from sams.datasource.base import a_share_symbol, classify_error
from sams.datasource.quote import field_float, parse_moment
from sams.datasource.sources import source

QUOTE_URL = "https://hq.sinajs.cn/list="
_LINE = re.compile(r'hq_str_([^=]+)="([^"]*)"')


def sina_quote_symbol(market: str, code: str) -> str:
    if market == "A":
        return a_share_symbol(code)
    if market == "HK":
        return f"hk{canonical_code('HK', code)}"
    if market == "US":
        return f"gb_{canonical_code('US', code).split('.')[0].lower()}"
    raise ValueError(f"未知市场 {market}")


def quote_get(url: str, params: dict[str, str], *, timeout: int = 15) -> str:
    import requests

    response = requests.get(
        url,
        params=params,
        timeout=timeout,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        },
    )
    if response.status_code == 429:
        raise RateLimited("sina_quote", "HTTP 429 被限流")
    response.raise_for_status()
    return response.content.decode("gbk", errors="replace")


def parse_sina_quotes(
    text: str,
    *,
    market: str,
    codes: list[str],
    now: datetime,
    zone: ZoneInfo,
) -> QuoteBatch:
    payloads: dict[str, list[str]] = {}
    for raw in (text or "").splitlines():
        match = _LINE.search(raw)
        if match is not None:
            payloads[match.group(1).strip().lower()] = match.group(2).split(",")
    if not payloads:
        raise EmptyResponse("sina_quote", f"{market} 报价响应里没有赋值语句")

    quotes: list[Quote] = []
    skipped: list[str] = []
    errors: list[str] = []
    for code in codes:
        canonical = canonical_code(market, code)
        symbol = sina_quote_symbol(market, canonical)
        cells = payloads.get(symbol.lower())
        if cells is None or not any(cell.strip() for cell in cells):
            skipped.append(canonical)
            continue
        # 回显校验就是键名：新浪 A/HK 的字段里没有代码，键是唯一的回显。
        # 键与请求的 symbol 不等时，这是另一个标的的报价——跳过，不能贴到这只票上
        try:
            quote = _to_quote(market, canonical, cells, now=now, zone=zone)
        except _BadRow as exc:
            errors.append(f"{canonical}: {exc}")
            continue
        quotes.append(quote)
    return QuoteBatch(tuple(quotes), tuple(skipped), tuple(errors))


class _BadRow(ValueError):
    pass


def _to_quote(market: str, code: str, cells: list[str], *, now: datetime, zone):
    if market == "A":
        return _a_quote(code, cells, now=now, zone=zone)
    if market == "HK":
        return _hk_quote(code, cells, now=now, zone=zone)
    if market == "US":
        return _us_quote(code, cells, now=now, zone=zone)
    raise ValueError(f"未知市场 {market}")


def _price(cells: list[str], index: int) -> float:
    value = field_float(cells, index)
    if value is None or value <= 0:
        raise _BadRow(f"报价价格不可用（{(cells[index] if index < len(cells) else '缺位')!r}）")
    return value


def _tap(cells: list[str], index: int) -> float | None:
    value = field_float(cells, index)
    return value if value and value > 0 else None


def _a_quote(code: str, cells: list[str], *, now: datetime, zone) -> Quote:
    if len(cells) < 32:
        raise _BadRow(f"A 股字段不足（{len(cells)} 个）")
    price = _price(cells, 3)
    prev = field_float(cells, 2)
    pct = (price / prev - 1) * 100 if prev and prev > 0 else None
    moment = parse_moment(
        f"{cells[30].strip()} {cells[31].strip()}", ("%Y-%m-%d %H:%M:%S",), zone
    )
    return Quote(
        market="A",
        code=code,
        ts=moment or now,
        price=price,
        open=field_float(cells, 1),
        prev_close=prev,
        high=field_float(cells, 4),
        low=field_float(cells, 5),
        pct_chg=pct,
        volume=field_float(cells, 8),
        amount=field_float(cells, 9),
        bid1=_tap(cells, 11),
        ask1=_tap(cells, 21),
        source="sina_quote",
        is_stale=moment is None,
    )


def _hk_quote(code: str, cells: list[str], *, now: datetime, zone) -> Quote:
    if len(cells) < 19:
        raise _BadRow(f"港股字段不足（{len(cells)} 个）")
    price = _price(cells, 6)
    moment = parse_moment(
        f"{cells[17].strip()} {cells[18].strip()}",
        ("%Y/%m/%d %H:%M", "%Y/%m/%d %H:%M:%S"),
        zone,
    )
    return Quote(
        market="HK",
        code=code,
        ts=moment or now,
        price=price,
        open=field_float(cells, 2),
        prev_close=field_float(cells, 3),
        high=field_float(cells, 4),
        low=field_float(cells, 5),
        pct_chg=field_float(cells, 8),
        volume=field_float(cells, 12),
        amount=field_float(cells, 11),
        bid1=_tap(cells, 9),
        ask1=_tap(cells, 10),
        source="sina_quote",
        is_stale=moment is None,
    )


def _us_quote(code: str, cells: list[str], *, now: datetime, zone) -> Quote:
    if len(cells) < 31:
        raise _BadRow(f"美股字段不足（{len(cells)} 个）")
    price = _price(cells, 1)
    moment = parse_moment(cells[3], ("%Y-%m-%d %H:%M:%S",), zone)
    return Quote(
        market="US",
        code=code,
        ts=moment or now,
        price=price,
        open=field_float(cells, 5),
        prev_close=field_float(cells, 26),
        high=field_float(cells, 6),
        low=field_float(cells, 7),
        pct_chg=field_float(cells, 2),
        volume=field_float(cells, 10),
        amount=field_float(cells, 30),
        source="sina_quote",
        is_stale=moment is None,
    )


class SinaQuoteAdapter:
    name = "sina_quote"
    markets = frozenset({"A", "HK", "US"})

    def __init__(
        self,
        *,
        quote_fetch: Callable[..., str] = quote_get,
        timeout: int = 15,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._quote_fetch = quote_fetch
        self._timeout = timeout
        self._clock = clock

    def quotes(self, market: str, codes: list[str]) -> QuoteBatch:
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        wanted = list(dict.fromkeys(canonical_code(market, code) for code in codes))
        if not wanted:
            return QuoteBatch()
        symbols = [sina_quote_symbol(market, code) for code in wanted]
        try:
            text = self._quote_fetch(
                QUOTE_URL, {"list": ",".join(symbols)}, timeout=self._timeout
            )
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc
        return parse_sina_quotes(
            text,
            market=market,
            codes=wanted,
            now=self._clock.now_utc(),
            zone=MARKET_ZONES[market],
        )


source(
    "sina_quote",
    options=("timeout",),
    note="新浪 hq.sinajs.cn 批量报价，A/HK/US，只提供报价",
)(SinaQuoteAdapter)
