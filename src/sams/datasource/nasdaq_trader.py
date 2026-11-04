"""美股代码表：NASDAQ Trader 的两份官方清单（D226）。

`nasdaqlisted.txt` 与 `otherlisted.txt` 是交易所自己发布的在市清单，不含行情、
不受 yfinance 限流影响。它们一次答全表，字段用竖线分隔，表尾那行
`File Creation Time` 不是数据行。

这里剔掉的四类东西不是普通股：ETF、权证/权利/单位、优先股/票据、测试代码。
它们的价格与倍数口径和普通股不可比，留在票池里会让「PE ≤ 10」这类条件把一张
权证算成一家公司。代码形态不合法的行同样丢掉——`BAC$A` 这种优先股代码在腾讯
报价里不是同一条通道，留着只会变成快照里的「源没答」。
"""

from __future__ import annotations

import re
from collections.abc import Callable

import polars as pl

from sams.core.errors import DatasourceError, EmptyResponse, FieldDrift
from sams.datasource.base import classify_error
from sams.datasource.sources import source

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

#: 普通股之外的证券名。`\b` 是必须的：`United` 里也有 `unit`，删掉它等于删掉一家公司
_NOT_COMMON = re.compile(
    r"\b(warrants?|rights?|units?|preferred|notes?|debentures?)\b", re.IGNORECASE
)
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")


def _http_text(url: str, *, timeout: int = 30) -> str:
    import requests

    response = requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    # 两份文件是 ASCII；用 utf-8 而不是 gbk，避免把公司名里的 & 之类的符号解坏
    return response.text


def _column(header: list[str], name: str, *, where: str) -> int:
    try:
        return [item.strip().lower() for item in header].index(name.lower())
    except ValueError:
        raise FieldDrift(
            "nasdaq_trader",
            f"{where} 缺少列 {name!r}：表头是 {header}（接口可能改版）",
        ) from None


def _parse(text: str, *, where: str, symbol: str, etf: str, test: str) -> list[dict]:
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return []
    header = lines[0].split("|")
    positions = {
        "symbol": _column(header, symbol, where=where),
        "name": _column(header, "Security Name", where=where),
        "etf": _column(header, etf, where=where),
        "test": _column(header, test, where=where),
    }
    rows: list[dict] = []
    seen: set[str] = set()
    for line in lines[1:]:
        if line.startswith("File Creation Time"):
            continue
        fields = line.split("|")
        if len(fields) <= max(positions.values()):
            continue
        code = fields[positions["symbol"]].strip().upper()
        name = fields[positions["name"]].strip()
        if not _SYMBOL.fullmatch(code):
            continue
        if fields[positions["test"]].strip().upper() == "Y":
            continue
        if fields[positions["etf"]].strip().upper() == "Y":
            continue
        if _NOT_COMMON.search(name):
            continue
        if code in seen:
            continue
        seen.add(code)
        rows.append({"code": code, "name": name})
    return rows


def parse_nasdaq_listed(text: str) -> list[dict]:
    return _parse(
        text, where="nasdaqlisted.txt", symbol="Symbol", etf="ETF", test="Test Issue"
    )


def parse_other_listed(text: str) -> list[dict]:
    return _parse(
        text, where="otherlisted.txt", symbol="ACT Symbol", etf="ETF", test="Test Issue"
    )


class NasdaqTraderAdapter:
    name = "nasdaq_trader"
    markets = frozenset({"US"})

    def __init__(
        self,
        nasdaq_fetch: Callable[[], str] = lambda: _http_text(NASDAQ_LISTED_URL),
        other_fetch: Callable[[], str] = lambda: _http_text(OTHER_LISTED_URL),
    ) -> None:
        self._nasdaq_fetch = nasdaq_fetch
        self._other_fetch = other_fetch

    def symbol_list(self, market: str) -> pl.DataFrame:
        """两份文件都要答：只拿到一半的清单会让 `deactivate_missing` 把另一半关掉。"""
        if market not in self.markets:
            raise EmptyResponse(self.name, f"美股代码表不覆盖 {market} 市场")
        try:
            nasdaq = parse_nasdaq_listed(self._nasdaq_fetch())
            other = parse_other_listed(self._other_fetch())
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc
        if not nasdaq or not other:
            missing = "nasdaqlisted.txt" if not nasdaq else "otherlisted.txt"
            raise EmptyResponse(self.name, f"美股代码表 {missing} 没有可用行")
        rows = nasdaq + other
        return pl.DataFrame(rows, schema={"code": pl.Utf8, "name": pl.Utf8})


source(
    "nasdaq_trader",
    note="NASDAQ Trader 官方在市清单（普通股；仅 US）",
)(NasdaqTraderAdapter)
