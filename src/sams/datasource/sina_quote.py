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
from sams.core.codes import canonical_code, looks_like_index_code
from sams.core.errors import DatasourceError, EmptyResponse, RateLimited
from sams.core.quotes import MARKET_ZONES, Quote, QuoteBatch
from sams.datasource.base import a_share_symbol, classify_error
from sams.datasource.quote import field_float, parse_moment
from sams.datasource.sources import source

#: 不能带路径里那个 `=`：requests 会把 params 拼成查询串，`.../list=` 会变成
#: `.../list=?list=...`，新浪把 `?list=...` 当成代码原样回显——每只票都是空行，
#: 整批静默 skipped（live 冒烟抓出来的）。`/list?list=...` 实测三个市场都正常回键。
QUOTE_URL = "https://hq.sinajs.cn/list"
_LINE = re.compile(r'hq_str_([^=]+)="([^"]*)"')

#: 美股代码里会被剥掉的交易所后缀（大写）。仓库里查得到的美股后缀形态只有：
#: 腾讯 smartbox 解析的文档 `parse_us_hint`（tencent.py）给出 `ba.n`，
#: 测试夹具出现 `usAAPL.OQ`（test_tencent.py）与回显 `BRK.A.N`（test_quote_helpers.py）。
#: 没有现成的后缀集，这里只收 `.OQ`（纳斯达克）、`.N`（纽交所）、`.TO`（多伦多）
#: 三种最常见形态。带点但不在这个集里的尾巴不是交易所而是类股（`BRK.B`）——
#: 判错方向是把 B 类股的报价贴到 A 类的键下，所以宁可拒绝。
_US_EXCHANGE_SUFFIXES = frozenset({".OQ", ".N", ".TO"})


def sina_quote_symbol(market: str, code: str) -> str:
    if market == "A":
        return a_share_symbol(code)
    if market == "HK":
        return f"hk{canonical_code('HK', code)}"
    if market == "US":
        canonical = canonical_code("US", code)
        base, _, tail = canonical.partition(".")
        if tail and f".{tail}" not in _US_EXCHANGE_SUFFIXES:
            # 类股（BRK.B）：实测新浪 `gb_brk_b`/`gb_brk_a` 都是空行、`gb_brkb` 是死行，
            # 而按点前裸代码发 `gb_brk` 拿到的是 A 类的报价且毫无异常。新浪的载荷里
            # 没有代码字段，键就是回显——`echo_matches` 救不了构造期，只能在这里拒绝，
            # 让适配器把它记进 skipped，而不是拿别的类股的价格来顶。
            raise ValueError(f"新浪报价的键表达不了美股同类股 {code}")
        return f"gb_{base.lower()}"
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


def _to_quote(
    market: str, code: str, cells: list[str], *, now: datetime, zone: ZoneInfo
) -> Quote:
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
        # 「缺位」分支到不了：每个市场入口先按字段表设长度下限（32/19/31），都盖过价格位
        raise _BadRow(f"报价价格不可用（{cells[index]!r}）")
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
        # 新浪报价不覆盖指数（D145）：指数码不进请求、直接进 skipped，不记 errors
        index_skipped = list(
            dict.fromkeys(code.strip() for code in codes if looks_like_index_code(market, code))
        )
        wanted = list(
            dict.fromkeys(
                canonical_code(market, code)
                for code in codes
                if not looks_like_index_code(market, code)
            )
        )
        if not wanted:
            return QuoteBatch(skipped=tuple(index_skipped))
        symbols: list[str] = []
        quotable: list[str] = []
        pre_skipped: list[str] = []
        for code in wanted:
            try:
                symbols.append(sina_quote_symbol(market, code))
            except ValueError:
                # 这只票在这个源拿不到键，宁可出现在 skipped 也不能拿别的类股的价格
                pre_skipped.append(code)
            else:
                quotable.append(code)
        if not quotable:
            return QuoteBatch(skipped=tuple(pre_skipped) + tuple(index_skipped))
        try:
            text = self._quote_fetch(
                QUOTE_URL, {"list": ",".join(symbols)}, timeout=self._timeout
            )
            # 解析也要在 try 里：解析层的意外 bug 同样按 DatasourceError 分类上报，
            # 而不是以裸异常穿透到路由层（与 tencent 同一口径）
            batch = parse_sina_quotes(
                text,
                market=market,
                codes=quotable,
                now=self._clock.now_utc(),
                zone=MARKET_ZONES[market],
            )
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc
        # 预跳过的并到批次末尾：parse 只见过能成键的代码，它的 skipped 是「源没给」
        return QuoteBatch(
            batch.quotes,
            batch.skipped + tuple(pre_skipped) + tuple(index_skipped),
            batch.errors,
        )


source(
    "sina_quote",
    options=("timeout",),
    note="新浪 hq.sinajs.cn 批量报价，A/HK/US，只提供报价",
)(SinaQuoteAdapter)
