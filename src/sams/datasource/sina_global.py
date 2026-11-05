"""新浪港美股日线（akshare 封装的 `stock_hk_daily` / `stock_us_daily` / `index_us_stock_sina`）。

港美股日线原来只有 yfinance → 腾讯两跳，两家都在境外机房：同一次网络故障能把整条链
一起打掉。新浪这一路是第三个独立故障域（照旧是**不复权**：`adjust=""`）。

三条实测事实（2026-11-05，写成代码是因为每一条都决定了一个分支）：
  1. **港股** `stock_hk_daily(symbol="00700", adjust="")` → 5508 行 / 7 列，带成交额
     （2026-11-05：量 56,903,938 股、额 24,168,809,665 元）。symbol 必须是五位：
     `"700"` 抛 `KeyError: 'date'`——那不是「没数据」，是根本没问对票。
  2. **美股** `stock_us_daily("AAPL", adjust="")` → 10068 行 / 6 列，**没有成交额**。
     symbol 是裸代码：`"AAPL.OQ"` 抛 `IndexError`；`"BRK.B"` 这类类股照常可用。
  3. **美股指数** `index_us_stock_sina(".IXIC")` → 5747 行，有 `amount` 列但**整列为 0**。
     指数没有成交额，0 是缺省值而不是事实：存 0 会让「零成交」混进成交额统计，所以
     整列置 NULL，和腾讯分钟 K 的成交额一样宁缺勿错。

窗口裁剪在这里做：三个接口都一次给全历史，请求区间只是一道过滤。`start` 很老时
不会像 A 股 sina 那样「只给最近 N 根」——历史有多长就给多长。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import polars as pl

from sams.core.codes import canonical_code
from sams.core.errors import DatasourceError, EmptyResponse
from sams.datasource.base import classify_error, normalize_bars
from sams.datasource.sources import source

#: 美股代码里会被剥掉的交易所后缀。与 `sina_quote` 同一份判据：仓库里查得到的形态只有
#: `.OQ`（纳斯达克）、`.N`（纽交所）、`.TO`（多伦多）；带点但不在这个集里的尾巴是类股
#: （`BRK.B`），剥掉它等于把 B 类股问成 A 类。
_US_EXCHANGE_SUFFIXES = frozenset({".OQ", ".N", ".TO"})

#: 本系统的美股指数码 → 新浪符号。只收 `[indices]` 默认那三个；问不到的指数是答复，
#: 不是「去猜一个符号再问一次」。
US_INDEX_SYMBOLS = {"usINX": ".INX", "usIXIC": ".IXIC", "usDJI": ".DJI"}

COLUMN_MAP = {
    "date": "date",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "amount": "amount",
}


def sina_us_ticker(code: str) -> str:
    """本系统的美股代码 → `stock_us_daily` 要的裸代码。

    先剥**一个**尾巴：`BRK.A.N` 留下 `BRK.A`（A 类是标的属性，`.N` 才是交易所），
    `BRK.B` 原样保留。
    """
    canonical = canonical_code("US", code)
    base, _, tail = canonical.rpartition(".")
    if tail and f".{tail}" in _US_EXCHANGE_SUFFIXES:
        return base
    return canonical


def sina_global_daily(symbol: str, market: str) -> pl.DataFrame:
    """真实网络调用：港股/美股全历史日线（不复权）。`symbol` 已按市场构造好。"""
    import akshare as ak

    if market == "HK":
        frame = ak.stock_hk_daily(symbol=symbol, adjust="")
    elif market == "US":
        frame = ak.stock_us_daily(symbol=symbol, adjust="")
    else:
        raise ValueError(f"sina_global 不支持市场 {market}")
    if frame is None:
        return pl.DataFrame()
    return frame if isinstance(frame, pl.DataFrame) else pl.from_pandas(frame)


def sina_us_index_daily(symbol: str) -> pl.DataFrame:
    """真实网络调用：美股指数全历史日线。`symbol` 是新浪形态（`.IXIC`）。"""
    import akshare as ak

    frame = ak.index_us_stock_sina(symbol=symbol)
    if frame is None:
        return pl.DataFrame()
    return frame if isinstance(frame, pl.DataFrame) else pl.from_pandas(frame)


def parse_sina_global_daily(
    raw: pl.DataFrame,
    *,
    market: str,
    code: str,
    start: date,
    end: date,
    drop_amount: bool = False,
) -> pl.DataFrame:
    """纯解析：接口帧 → `BAR_COLUMNS`，按 `[start, end]` 裁剪并按入库形态重写 code。

    `drop_amount` 只给指数用：那一列整列为 0（接口的缺省值），留着就是「零成交额」。
    """
    if raw.is_empty():
        # 空帧没有列，直接交给 normalize_bars 会被判成「缺 date/close」的改版；
        # 接口对查不到的代码正是回一张空表，这是「没有记录」而不是「结构变了」
        raise EmptyResponse("sina_global", f"{market} {code} 无日线记录")
    frame = raw.drop("amount") if drop_amount and "amount" in raw.columns else raw
    out = normalize_bars(
        frame,
        market=market,
        code=code,
        source="sina_global",
        column_map=COLUMN_MAP,
        # 港美股都是「股」，不乘 100
        volume_scale=1.0,
    )
    out = out.filter(pl.col("date").is_between(start, end))
    if out.is_empty():
        raise EmptyResponse("sina_global", f"{market} {code} 在 {start}~{end} 内 0 根")
    # 指数表的键要源形态（usIXIC），`normalize_bars` 会把它折成 USIXIC（D145）
    if drop_amount:
        out = out.with_columns(pl.lit(code).alias("code"))
    return out


class SinaGlobalAdapter:
    name = "sina_global"
    markets = frozenset({"HK", "US"})

    def __init__(
        self,
        *,
        daily_fetch: Callable[[str, str], pl.DataFrame] = sina_global_daily,
        index_fetch: Callable[[str], pl.DataFrame] = sina_us_index_daily,
    ) -> None:
        self._daily_fetch = daily_fetch
        self._index_fetch = index_fetch

    def daily_bars(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        if end < start:
            raise ValueError(f"区间反向: {start} > {end}")
        symbol = canonical_code(market, code) if market == "HK" else sina_us_ticker(code)
        try:
            raw = self._daily_fetch(symbol, market)
            return parse_sina_global_daily(
                raw, market=market, code=code, start=start, end=end
            )
        except DatasourceError:
            raise  # 已经是本域异常，别二次归类
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc

    def index_daily(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        """美股指数日线。港股指数这一路没有：`EmptyResponse` 是答复，让链继续问下一个源。"""
        if market != "US":
            raise EmptyResponse(self.name, f"新浪这一路没有 {market} 指数日线")
        if end < start:
            raise ValueError(f"区间反向: {start} > {end}")
        symbol = US_INDEX_SYMBOLS.get(code)
        if symbol is None:
            raise EmptyResponse(self.name, f"新浪不认识美股指数代码 {code}")
        try:
            raw = self._index_fetch(symbol)
            return parse_sina_global_daily(
                raw, market="US", code=code, start=start, end=end, drop_amount=True
            )
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc


source(
    "sina_global",
    note="新浪港美股日线（HK/US 全历史，不复权；US 指数 .INX/.IXIC/.DJI）",
)(SinaGlobalAdapter)
