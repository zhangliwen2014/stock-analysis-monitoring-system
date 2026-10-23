"""腾讯行情（gtimg）日线源：A 股 / 港股 / 美股三市场通用备源。

为什么值得单独接：它是本机唯一同时满足「三个市场都覆盖」且「不需要 API key、
不需要 cookie、不看 referer 签名」的免费接口。港美股此前只有 yfinance 一个源，
主源被限流时系统就瞎了（spec §7.1 的 U5）——这个适配器把那条单点变成两级。

行序是 `[日期, 开, 收, 高, 低, 量]`：**收在第三列而不是第二列**。
按位置取数时必须照着这个顺序，写错一位会把开盘价当收盘价存进库，
而这样的数据看起来完全正常。

给的是不复权价（用 `kline/kline` 与 `fqkline/get` 的空复权位）——
spec §5.5 定死库里只存 raw。若哪天要 qfq，去请求 `...,qfq` 拿的是 `qfqday` 键，
那种响应本模块**刻意拒绝**：同一个源两种口径，一旦混进库就再也分不出哪根是哪个。

"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date, datetime
from zoneinfo import ZoneInfo

import polars as pl

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_code
from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift, RateLimited
from sams.core.quotes import MARKET_ZONES, Quote, QuoteBatch
from sams.datasource.base import (
    LOTS_TO_SHARES,
    a_share_symbol,
    classify_error,
    normalize_bars,
)
from sams.datasource.quote import echo_matches, field_float, parse_moment
from sams.datasource.sources import source

KLINE_PATHS = {
    # A 股走 fqkline（复权位留空 = 不复权）；港美股走 kline/kline
    "A": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    "HK": "https://web.ifzq.gtimg.cn/appstock/app/kline/kline",
    "US": "https://web.ifzq.gtimg.cn/appstock/app/kline/kline",
}
SMARTBOX_URL = "https://smartbox.gtimg.cn/s3/"
#: 行内六个字段的含义（腾讯给的顺序，收盘价在第 3 个）
FIELD_ORDER = ("date", "open", "close", "high", "low", "volume")
QUOTE_URL = "https://qt.gtimg.cn/q"
_TENCENT_LINE = re.compile(r'v_([^=]+)="([^"]*)"')
#: 三个市场的时间格式各不相同：同一列 30 的字符串，A 股没有分隔符，港股是斜杠
_QUOTE_TS_FORMATS = {
    "A": ("%Y%m%d%H%M%S",),
    "HK": ("%Y/%m/%d %H:%M:%S",),
    "US": ("%Y-%m-%d %H:%M:%S",),
}


def tencent_quote_symbol(market: str, code: str) -> str:
    """报价接口的 symbol 与日线不同：美股**不能**带交易所后缀。

    实测 `usAAPL.OQ` 返回空行，而 `usAAPL` 返回正常报价且字段里的代码仍是
    `AAPL.OQ`。所以这里只取裸代码，回显校验由 `echo_matches` 按点前比较。
    """
    if market == "A":
        return tencent_symbol(market, code)
    if market == "HK":
        return tencent_symbol(market, code)
    # 美股报价必须剥掉交易所后缀（与日线共用 symbol 会拿到空行），所以不走 tencent_symbol
    if market == "US":
        return f"us{canonical_code('US', code).split('.')[0]}"
    raise ValueError(f"未知市场 {market}")


def tencent_symbol(market: str, code: str) -> str:
    """本系统代码 → 腾讯 symbol：`sh600519` / `hk00700` / `usAAPL.OQ`。

    美股必须带交易所后缀（`usAAPL` 会返回空 day 数组——把「后缀不对」伪装成
    「没有数据」是最坏的一种静默），所以美股那一跳要先经 smartbox 解析，
    调用方通过 `resolve_us_suffix` 注入。
    """
    if market == "A":
        return a_share_symbol(code)
    if market == "HK":
        return f"hk{canonical_code('HK', code)}"
    if market == "US":
        return str(code)
    raise ValueError(f"未知市场 {market}")


def parse_us_hint(payload_text: str, symbol: str) -> str:
    """从 smartbox 的 `v_hint` 里挑出交易所后缀。

    返回值形如 `us~aapl.oq~苹果~pg~GP`，多个候选用 `^` 分隔。只要**裸代码完全相等**的那个：
    查 `BA` 时第一个候选就是 `ba.n`，但查 `B` 时 `b.n` 与 `ba-a.n` 都在列表里，
    取错一个就拿到另一只票的历史。
    """
    want = symbol.upper()
    text = payload_text or ""
    body = text.split('"', 1)[1] if '"' in text else ""
    candidates = []
    for chunk in body.split("^"):
        parts = chunk.split("~")
        if len(parts) < 2 or not parts[0].startswith("us"):
            continue
        tail = parts[1].removeprefix("us").upper()
        base, _, suffix = tail.partition(".")
        if base == want and suffix:
            candidates.append(f"us{want}.{suffix}")
    if not candidates:
        raise EmptyResponse("tencent", f"腾讯未找到美股 {symbol}（代码或交易所后缀不对）")
    return candidates[0]


def parse_tencent_daily(payload: object, *, market: str, code: str, symbol: str) -> pl.DataFrame:
    """纯解析：腾讯 kline JSON → 标准列集 DataFrame。

    `symbol` 是本次实际请求的那个键，响应用它来取——`next(iter(data.values()))` 看着等价，
    但接口在参数被改写、代码段重合时会把**另一只票**的序列放回同一个节点里，而那条序列
    字段齐全、日期连续、看起来完全正常。错一位就是把别家公司十年的收盘价存进这只票的键下，
    事后没有任何办法在库里查出来。
    """
    if not isinstance(payload, dict):
        raise FieldDrift("tencent", f"响应不是对象: {type(payload).__name__}")
    if payload.get("code") not in (0, None):
        # {"code":1,"msg":"bad params"}：参数或代码不对，不是「今天没数据」
        raise FieldDrift("tencent", f"接口拒绝请求: {payload.get('msg') or payload}")
    data = payload.get("data")
    if not isinstance(data, dict) or not data:
        raise EmptyResponse("tencent", f"{market} {code} 区间无数据")
    body = data.get(symbol)
    if body is None:
        raise FieldDrift("tencent", f"响应的不是 {symbol}，实际是 {sorted(data)}")
    if not isinstance(body, dict):
        raise FieldDrift("tencent", f"data 节点形态不对: {type(body).__name__}")
    if "day" not in body:
        # qfqday/hfqday 是复权序列，库里不收（spec §5.5）
        raise FieldDrift(
            "tencent", f"响应不含 raw 的 day 键，实际键 {sorted(body)}；复权序列不入库"
        )
    rows = body["day"]
    if not isinstance(rows, list) or not rows:
        raise EmptyResponse("tencent", f"{market} {code} 区间无数据")

    records = []
    for row in rows:
        # 港股行在第 6 个字段后还挂着一个公司行为对象（回购/派息），按前六个字段取
        if not isinstance(row, list) or len(row) < len(FIELD_ORDER):
            raise FieldDrift("tencent", f"kline 行字段不足: {row!r}")
        records.append(dict(zip(FIELD_ORDER, row[: len(FIELD_ORDER)], strict=True)))

    frame = pl.DataFrame(records)
    return normalize_bars(
        frame,
        market=market,
        code=code,
        source="tencent",
        column_map={k: k for k in FIELD_ORDER},
        # 腾讯 A 股给手、港美股给股（腾讯把 00700 单日写成 23354069，是股）
        volume_scale=LOTS_TO_SHARES if market == "A" else 1.0,
    )


def parse_tencent_quotes(
    text: str,
    *,
    market: str,
    codes: list[str],
    now: datetime,
    zone: ZoneInfo,
) -> QuoteBatch:
    """腾讯 `v_xxx="..."` 批量响应 → QuoteBatch。

    字段位置是接口的硬事实：3 价、4 昨收、5 开、6 量、30 时间、32 涨跌幅、
    33 高、34 低、9 买一、19 卖一。A 股的量是「手」，港美股是「股」；
    A 股的成交额藏在第 35 个复合字段的第三段，港美股直接给在第 37 位。
    """
    if not (text or "").strip():
        raise EmptyResponse("tencent", f"{market} 报价响应为空")
    parsed: list[list[str]] = []
    for raw in text.splitlines():
        match = _TENCENT_LINE.search(raw)
        if match is not None and match.group(2).strip():
            parsed.append(match.group(2).split("~"))
    if not parsed:
        raise EmptyResponse("tencent", f"{market} 报价响应里没有可读的行")

    quotes: list[Quote] = []
    skipped: list[str] = []
    errors: list[str] = []
    for code in codes:
        canonical = canonical_code(market, code)
        row = next(
            (
                fields
                for fields in parsed
                if len(fields) > 2 and echo_matches(market, canonical, fields[2])
            ),
            None,
        )
        if row is None:
            skipped.append(canonical)
            continue
        if len(row) < 35:
            errors.append(f"{canonical}: 字段不足（{len(row)} 个）")
            continue
        price = field_float(row, 3)
        if price is None or price <= 0:
            errors.append(f"{canonical}: 报价价格不可用（{row[3]!r}）")
            continue
        moment = parse_moment(row[30], _QUOTE_TS_FORMATS[market], zone)
        volume = field_float(row, 6)
        if volume is not None and market == "A":
            volume *= LOTS_TO_SHARES
        # 复合字段只在 A 股的第 35 位，且行可能短于它；港股/美股直接读第 37 位。
        # 越界会抛 IndexError，而这里要的是「没有这一格」→ None
        if market == "A":
            amount = field_float(row[35].split("/"), 2) if len(row) > 35 else None
        else:
            amount = field_float(row, 37)
        bid = field_float(row, 9)
        ask = field_float(row, 19)
        quotes.append(
            Quote(
                market=market,
                code=canonical,
                ts=moment or now,
                price=price,
                open=field_float(row, 5),
                prev_close=field_float(row, 4),
                high=field_float(row, 33),
                low=field_float(row, 34),
                pct_chg=field_float(row, 32),
                volume=volume,
                amount=amount,
                bid1=bid if bid and bid > 0 else None,
                ask1=ask if ask and ask > 0 else None,
                source="tencent",
                is_stale=moment is None,
            )
        )
    return QuoteBatch(tuple(quotes), tuple(skipped), tuple(errors))


def http_get(url: str, params: dict[str, str], *, timeout: int = 15) -> dict:
    import requests

    response = requests.get(
        url,
        params=params,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
    )
    if response.status_code == 429:
        # 429 必须是 RateLimited 而不是笼统的 DatasourceError：
        # 路由器对前者退避重试、对后者立刻换源，限流时换源等于把备源也打爆
        raise RateLimited("tencent", "HTTP 429 被限流")
    response.raise_for_status()
    return json.loads(response.text)


def smartbox_get(url: str, params: dict[str, str], *, timeout: int = 15) -> str:
    """代码解析接口返回的是 JS 赋值语句，不是 JSON，只能按文本取。"""
    import requests

    response = requests.get(
        url,
        params=params,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
    )
    response.raise_for_status()
    return response.text


def quote_get(url: str, params: dict[str, str], *, timeout: int = 15) -> str:
    """腾讯报价是 GBK 的 JS 赋值语句，不是 JSON。"""
    import requests

    response = requests.get(
        url,
        params=params,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
    )
    if response.status_code == 429:
        raise RateLimited("tencent", "HTTP 429 被限流")
    response.raise_for_status()
    return response.content.decode("gbk", errors="replace")


class TencentAdapter:
    """一个实现覆盖三个市场；`markets` 如实声明，registry 据此校验路由合法性。"""

    name = "tencent"
    markets = frozenset({"A", "HK", "US"})

    def __init__(
        self,
        *,
        get: Callable[..., dict] = http_get,
        hint_get: Callable[..., str] = smartbox_get,
        resolve: Callable[[str], str] | None = None,
        timeout: int = 15,
        quote_fetch: Callable[..., str] = quote_get,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._get = get
        self._hint_get = hint_get
        self._timeout = timeout
        self._hints: dict[str, str] = {}
        self._resolve = resolve or self._smartbox_resolve
        self._quote_fetch = quote_fetch
        self._clock = clock

    def _smartbox_resolve(self, symbol: str) -> str:
        text = self._hint_get(
            SMARTBOX_URL, {"v": "2", "q": symbol, "t": "us"}, timeout=self._timeout
        )
        return parse_us_hint(text, symbol)

    def _us_symbol(self, symbol: str) -> str:
        """后缀解析结果缓存：一次全市场回补不该为同一只票重复问 smartbox。"""
        if symbol not in self._hints:
            self._hints[symbol] = self._resolve(symbol)
        return self._hints[symbol]

    def daily_bars(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        if end < start:
            raise ValueError(f"区间反向: {start} > {end}")
        symbol = tencent_symbol(market, code)
        if market == "US":
            symbol = self._us_symbol(symbol)
        params = {
            # 逗号分隔的一串参数：symbol, 周期, 起, 止, 根数[, 复权位]
            # A 股走 fqkline，复权位必须**留空但保留**（少这一位会被接口按默认口径处理，
            # 而 kline/kline 那条路又不接受第六位——两个端点的参数形状不一样）
            "param": kline_param(market, symbol, start, end, _bars_for(start, end))
        }
        try:
            payload = self._get(KLINE_PATHS[market], params, timeout=self._timeout)
            return parse_tencent_daily(payload, market=market, code=code, symbol=symbol)
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc

    def quotes(self, market: str, codes: list[str]) -> QuoteBatch:
        """一次请求的代码清单由调用方按 `QUOTE_BATCH` 分批（service 层负责），本方法不切片。"""
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        wanted = list(dict.fromkeys(canonical_code(market, code) for code in codes))
        if not wanted:
            return QuoteBatch()
        symbols = [tencent_quote_symbol(market, code) for code in wanted]
        try:
            text = self._quote_fetch(
                QUOTE_URL, {"q": ",".join(symbols)}, timeout=self._timeout
            )
            # 解析也要在 try 里：解析层的意外 bug 同样按 DatasourceError 分类上报，
            # 而不是以裸异常穿透到路由层（与 daily_bars 同一口径）
            return parse_tencent_quotes(
                text,
                market=market,
                codes=wanted,
                now=self._clock.now_utc(),
                zone=MARKET_ZONES[market],
            )
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc


def kline_param(market: str, symbol: str, start: date, end: date, count: int) -> str:
    base = f"{symbol},day,{start:%Y-%m-%d},{end:%Y-%m-%d},{count}"
    return f"{base}," if market == "A" else base


def _bars_for(start: date, end: date) -> int:
    """请求的根数按日历天给足：交易日只会更少，按交易日给会静默截断成一段缺口。

    腾讯的上限没写在文档里，所以取到日历天数还留点余量；真被截断时
    `test_tencent_range_is_not_truncated` 那条 live 用例会红，而不是让缺口静默入库。
    """
    return (end - start).days + 5


# 放在模块末尾：装饰器要等类定义完才拿得到 markets
source("tencent", options=("timeout",), note="腾讯 gtimg 日线，A/HK/US 三市场，不复权")(
    TencentAdapter
)
